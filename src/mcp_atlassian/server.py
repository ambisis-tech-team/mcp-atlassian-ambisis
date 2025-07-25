import json
import logging
import os
from collections.abc import AsyncIterator, Sequence
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any
from urllib.parse import urlparse

from mcp.server import Server
from mcp.types import Resource, TextContent, Tool

from .confluence import ConfluenceFetcher
from .jira import JiraFetcher

# Configure logging
logger = logging.getLogger("mcp-atlassian")


@dataclass
class AppContext:
    """Application context for MCP Atlassian."""

    confluence: ConfluenceFetcher | None = None
    jira: JiraFetcher | None = None


def get_available_services() -> dict[str, bool | None]:
    """Determine which services are available based on environment variables."""

    # Check for either cloud authentication (URL + username + API token)
    # or server/data center authentication (URL + personal token)
    confluence_url = os.getenv("CONFLUENCE_URL")
    if confluence_url:
        is_cloud = "atlassian.net" in confluence_url
        if is_cloud:
            confluence_vars = all(
                [
                    confluence_url,
                    os.getenv("CONFLUENCE_USERNAME"),
                    os.getenv("CONFLUENCE_API_TOKEN"),
                ]
            )
            logger.info("Using Confluence Cloud authentication method")
        else:
            confluence_vars = all(
                [confluence_url, os.getenv("CONFLUENCE_PERSONAL_TOKEN")]
            )
            logger.info("Using Confluence Server/Data Center authentication method")
    else:
        confluence_vars = False

    # Check for either cloud authentication (URL + username + API token)
    # or server/data center authentication (URL + personal token)
    jira_url = os.getenv("JIRA_URL")
    if jira_url:
        is_cloud = "atlassian.net" in jira_url
        if is_cloud:
            jira_vars = all(
                [jira_url, os.getenv("JIRA_USERNAME"), os.getenv("JIRA_API_TOKEN")]
            )
            logger.info("Using Jira Cloud authentication method")
        else:
            jira_vars = all([jira_url, os.getenv("JIRA_PERSONAL_TOKEN")])
            logger.info("Using Jira Server/Data Center authentication method")
    else:
        jira_vars = False

    return {"confluence": confluence_vars, "jira": jira_vars}


@asynccontextmanager
async def server_lifespan(server: Server) -> AsyncIterator[AppContext]:
    """Initialize and clean up application resources."""
    # Get available services
    services = get_available_services()

    try:
        # Initialize services
        confluence = ConfluenceFetcher() if services["confluence"] else None
        jira = JiraFetcher() if services["jira"] else None

        # Log the startup information
        logger.info("Starting MCP Atlassian server")
        if confluence:
            confluence_url = confluence.config.url
            logger.info(f"Confluence URL: {confluence_url}")
        if jira:
            jira_url = jira.config.url
            logger.info(f"Jira URL: {jira_url}")

        # Provide context to the application
        yield AppContext(confluence=confluence, jira=jira)
    finally:
        # Cleanup resources if needed
        pass


# Create server instance
app = Server("mcp-atlassian", lifespan=server_lifespan)


# Implement server handlers
@app.list_resources()
async def list_resources() -> list[Resource]:
    """List Confluence spaces and Jira projects the user is actively interacting with."""
    resources = []

    ctx = app.request_context.lifespan_context

    # Add Confluence spaces the user has contributed to
    if ctx and ctx.confluence:
        try:
            # Get spaces the user has contributed to
            spaces = ctx.confluence.get_user_contributed_spaces(limit=250)

            # Add spaces to resources
            resources.extend(
                [
                    Resource(
                        uri=f"confluence://{space['key']}",
                        name=f"Confluence Space: {space['name']}",
                        mimeType="text/plain",
                        description=(
                            f"A Confluence space containing documentation and knowledge base articles. "
                            f"Space Key: {space['key']}. "
                            f"{space.get('description', '')} "
                            f"Access content using: confluence://{space['key']}/pages/PAGE_TITLE"
                        ).strip(),
                    )
                    for space in spaces.values()
                ]
            )
        except Exception as e:
            logger.error(f"Error fetching Confluence spaces: {str(e)}")

    # Add Jira projects the user is involved with
    if ctx and ctx.jira:
        try:
            # Get current user's account ID
            account_id = ctx.jira.get_current_user_account_id()

            # Use JQL to find issues the user is assigned to or reported
            jql = f"assignee = {account_id} OR reporter = {account_id} ORDER BY updated DESC"
            issues = ctx.jira.jira.jql(jql, limit=250, fields=["project"])

            # Extract and deduplicate projects
            projects = {}
            for issue in issues.get("issues", []):
                project = issue.get("fields", {}).get("project", {})
                project_key = project.get("key")
                if project_key and project_key not in projects:
                    projects[project_key] = {
                        "key": project_key,
                        "name": project.get("name", project_key),
                        "description": project.get("description", ""),
                    }

            # Add projects to resources
            resources.extend(
                [
                    Resource(
                        uri=f"jira://{project['key']}",
                        name=f"Jira Project: {project['name']}",
                        mimeType="text/plain",
                        description=(
                            f"A Jira project tracking issues and tasks. Project Key: {project['key']}. "
                        ).strip(),
                    )
                    for project in projects.values()
                ]
            )
        except Exception as e:
            logger.error(f"Error fetching Jira projects: {str(e)}")

    return resources


@app.read_resource()
async def read_resource(uri: str) -> tuple[str, str]:
    """Read content from Confluence based on the resource URI."""
    parsed_uri = urlparse(uri)

    # Get application context
    ctx = app.request_context.lifespan_context

    # Handle Confluence resources
    if uri.startswith("confluence://"):
        if not ctx or not ctx.confluence:
            raise ValueError(
                "Confluence is not configured. Please provide Confluence credentials."
            )
        parts = uri.replace("confluence://", "").split("/")

        # Handle space listing
        if len(parts) == 1:
            space_key = parts[0]

            # Use CQL to find recently updated pages in this space
            cql = f'space = "{space_key}" AND contributor = currentUser() ORDER BY lastmodified DESC'
            pages = ctx.confluence.search(cql=cql, limit=20)

            if not pages:
                # Fallback to regular space pages if no user-contributed pages found
                pages = ctx.confluence.get_space_pages(space_key, limit=10)

            content = []
            for page in pages:
                page_dict = page.to_simplified_dict()
                title = page_dict.get("title", "Untitled")
                url = page_dict.get("url", "")

                content.append(f"# [{title}]({url})\n\n{page.page_content}\n\n---")

            return "\n\n".join(content), "text/markdown"

        # Handle specific page
        elif len(parts) >= 3 and parts[1] == "pages":
            space_key = parts[0]
            title = parts[2]
            page = ctx.confluence.get_page_by_title(space_key, title)

            if not page:
                raise ValueError(f"Page not found: {title}")

            return page.page_content, "text/markdown"

    # Handle Jira resources
    elif uri.startswith("jira://"):
        if not ctx or not ctx.jira:
            raise ValueError("Jira is not configured. Please provide Jira credentials.")
        parts = uri.replace("jira://", "").split("/")

        # Handle project listing
        if len(parts) == 1:
            project_key = parts[0]

            # Get current user's account ID
            account_id = ctx.jira.get_current_user_account_id()

            # Use JQL to find issues in this project that the user is involved with
            jql = f"project = {project_key} AND (assignee = {account_id} OR reporter = {account_id}) ORDER BY updated DESC"
            issues = ctx.jira.search_issues(jql=jql, limit=20)

            if not issues:
                # Fallback to recent issues if no user-related issues found
                issues = ctx.jira.get_project_issues(project_key, limit=10)

            content = []
            for issue in issues:
                issue_dict = issue.to_simplified_dict()
                key = issue_dict.get("key", "")
                summary = issue_dict.get("summary", "Untitled")
                url = issue_dict.get("url", "")
                status = issue_dict.get("status", {})
                status_name = status.get("name", "Unknown") if status else "Unknown"

                # Create a markdown representation of the issue
                issue_content = (
                    f"# [{key}: {summary}]({url})\nStatus: {status_name}\n\n"
                )
                if issue_dict.get("description"):
                    issue_content += f"{issue_dict.get('description')}\n\n"

                content.append(f"{issue_content}---")

            return "\n\n".join(content), "text/markdown"

        # Handle specific issue
        elif len(parts) >= 2:
            issue_key = parts[1] if len(parts) > 1 else parts[0]
            issue = ctx.jira.get_issue(issue_key)

            if not issue:
                raise ValueError(f"Issue not found: {issue_key}")

            issue_dict = issue.to_simplified_dict()
            markdown = f"# {issue_dict.get('key')}: {issue_dict.get('summary')}\n\n"

            if issue_dict.get("status"):
                status_name = issue_dict.get("status", {}).get("name", "Unknown")
                markdown += f"**Status:** {status_name}\n\n"

            if issue_dict.get("description"):
                markdown += f"{issue_dict.get('description')}\n\n"

            return markdown, "text/markdown"

    raise ValueError(f"Invalid resource URI: {uri}")


@app.list_tools()
async def list_tools() -> list[Tool]:
    """List available Confluence and Jira tools."""
    tools = []
    ctx = app.request_context.lifespan_context

    # Add Confluence tools if Confluence is configured
    if ctx and ctx.confluence:
        tools.extend(
            [
                Tool(
                    name="confluence_search",
                    description="Search Confluence content using simple terms or CQL",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "query": {
                                "type": "string",
                                "description": "Search query - can be either a simple text (e.g. 'project documentation') or a CQL query string. Examples of CQL:\n"
                                "- Basic search: 'type=page AND space=DEV'\n"
                                "- Search by title: 'title~\"Meeting Notes\"'\n"
                                "- Recent content: 'created >= \"2023-01-01\"'\n"
                                "- Content with specific label: 'label=documentation'\n"
                                "- Recently modified content: 'lastModified > startOfMonth(\"-1M\")'\n"
                                "- Content modified this year: 'creator = currentUser() AND lastModified > startOfYear()'\n"
                                "- Content you contributed to recently: 'contributor = currentUser() AND lastModified > startOfWeek()'\n"
                                "- Content watched by user: 'watcher = \"user@domain.com\" AND type = page'\n"
                                '- Exact phrase in content: \'text ~ "\\"Urgent Review Required\\"" AND label = "pending-approval"\'\n'
                                '- Title wildcards: \'title ~ "Minutes*" AND (space = "HR" OR space = "Marketing")\'\n',
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum number of results (1-50)",
                                "default": 10,
                                "minimum": 1,
                                "maximum": 50,
                            },
                        },
                        "required": ["query"],
                    },
                ),
                Tool(
                    name="confluence_get_page",
                    description="Get content of a specific Confluence page by ID",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "page_id": {
                                "type": "string",
                                "description": "Confluence page ID (numeric ID, can be found in the page URL). "
                                "For example, in the URL 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title', "
                                "the page ID is '123456789'",
                            },
                            "include_metadata": {
                                "type": "boolean",
                                "description": "Whether to include page metadata such as creation date, last update, version, and labels",
                                "default": True,
                            },
                        },
                        "required": ["page_id"],
                    },
                ),
                Tool(
                    name="confluence_get_page_children",
                    description="Get child pages of a specific Confluence page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "parent_id": {
                                "type": "string",
                                "description": "The ID of the parent page whose children you want to retrieve",
                            },
                            "expand": {
                                "type": "string",
                                "description": "Fields to expand in the response (e.g., 'version', 'body.storage')",
                                "default": "version",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum number of child pages to return (1-50)",
                                "default": 25,
                                "minimum": 1,
                                "maximum": 50,
                            },
                            "include_content": {
                                "type": "boolean",
                                "description": "Whether to include the page content in the response",
                                "default": False,
                            },
                        },
                        "required": ["parent_id"],
                    },
                ),
                Tool(
                    name="confluence_get_page_ancestors",
                    description="Get ancestor (parent) pages of a specific Confluence page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "page_id": {
                                "type": "string",
                                "description": "The ID of the page whose ancestors you want to retrieve",
                            },
                        },
                        "required": ["page_id"],
                    },
                ),
                Tool(
                    name="confluence_get_comments",
                    description="Get comments for a specific Confluence page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "page_id": {
                                "type": "string",
                                "description": "Confluence page ID (numeric ID, can be parsed from URL, "
                                "e.g. from 'https://example.atlassian.net/wiki/spaces/TEAM/pages/123456789/Page+Title' "
                                "-> '123456789')",
                            }
                        },
                        "required": ["page_id"],
                    },
                ),
                Tool(
                    name="confluence_create_page",
                    description="Create a new Confluence page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "space_key": {
                                "type": "string",
                                "description": "The key of the space to create the page in "
                                "(usually a short uppercase code like 'DEV', 'TEAM', or 'DOC')",
                            },
                            "title": {
                                "type": "string",
                                "description": "The title of the page",
                            },
                            "content": {
                                "type": "string",
                                "description": "The content of the page in Storage confluence format.",
                            },
                            "parent_id": {
                                "type": "string",
                                "description": "Optional parent page ID. If provided, this page "
                                "will be created as a child of the specified page",
                            },
                        },
                        "required": ["space_key", "title", "content"],
                    },
                ),
                Tool(
                    name="confluence_update_page",
                    description="Update an existing Confluence page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "page_id": {
                                "type": "string",
                                "description": "The ID of the page to update",
                            },
                            "title": {
                                "type": "string",
                                "description": "The new title of the page",
                            },
                            "content": {
                                "type": "string",
                                "description": "The new content of the page in Storage confluence format",
                            },
                            "is_minor_edit": {
                                "type": "boolean",
                                "description": "Whether this is a minor edit",
                                "default": False,
                            },
                            "version_comment": {
                                "type": "string",
                                "description": "Optional comment for this version",
                                "default": "",
                            },
                        },
                        "required": ["page_id", "title", "content"],
                    },
                ),
                Tool(
                    name="confluence_delete_page",
                    description="Delete an existing Confluence page",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "page_id": {
                                "type": "string",
                                "description": "The ID of the page to delete",
                            },
                        },
                        "required": ["page_id"],
                    },
                ),
            ]
        )

    # Add Jira tools if Jira is configured
    if ctx and ctx.jira:
        tools.extend(
            [
                Tool(
                    name="jira_get_issue",
                    description=(
                        "Get details of a specific Jira issue including its Epic links "
                        "and relationship information"
                    ),
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                            "expand": {
                                "type": "string",
                                "description": (
                                    "Optional fields to expand. Examples: 'renderedFields' "
                                    "(for rendered content), 'transitions' (for available "
                                    "status transitions), 'changelog' (for history)"
                                ),
                                "default": None,
                            },
                            "comment_limit": {
                                "type": "integer",
                                "description": (
                                    "Maximum number of comments to include "
                                    "(0 or null for no comments)"
                                ),
                                "minimum": 0,
                                "maximum": 100,
                                "default": None,
                            },
                        },
                        "required": ["issue_key"],
                    },
                ),
                Tool(
                    name="jira_search",
                    description="Search Jira issues using JQL (Jira Query Language)",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "jql": {
                                "type": "string",
                                "description": "JQL query string (Jira Query Language). Examples:\n"
                                '- Find Epics: "issuetype = Epic AND project = PROJ"\n'
                                '- Find issues in Epic: "parent = PROJ-123"\n'
                                "- Find by status: \"status = 'In Progress' AND project = PROJ\"\n"
                                '- Find by assignee: "assignee = currentUser()"\n'
                                '- Find recently updated: "updated >= -7d AND project = PROJ"\n'
                                '- Find by label: "labels = frontend AND project = PROJ"\n'
                                '- Find by priority: "priority = High AND project = PROJ"',
                            },
                            "fields": {
                                "type": "string",
                                "description": (
                                    "Comma-separated fields to return in the results. "
                                    "Use '*all' for all fields, or specify individual "
                                    "fields like 'summary,status,assignee,priority'"
                                ),
                                "default": "*all",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum number of results (1-50)",
                                "default": 10,
                                "minimum": 1,
                                "maximum": 50,
                            },
                        },
                        "required": ["jql"],
                    },
                ),
                Tool(
                    name="jira_get_project_issues",
                    description="Get all issues for a specific Jira project",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "The project key",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum number of results (1-50)",
                                "default": 10,
                                "minimum": 1,
                                "maximum": 50,
                            },
                        },
                        "required": ["project_key"],
                    },
                ),
                Tool(
                    name="jira_create_issue",
                    description="Create a new Jira issue with optional Epic link or parent for subtasks",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "The JIRA project key (e.g. 'PROJ', 'DEV', 'SUPPORT'). "
                                "This is the prefix of issue keys in your project. "
                                "Never assume what it might be, always ask the user.",
                            },
                            "summary": {
                                "type": "string",
                                "description": "Summary/title of the issue",
                            },
                            "issue_type": {
                                "type": "string",
                                "description": (
                                    "Issue type (e.g. 'Task', 'Bug', 'Story', 'Epic', 'Subtask'). "
                                    "The available types depend on your project configuration. "
                                    "For subtasks, use 'Subtask' (not 'Sub-task') and include parent in additional_fields."
                                ),
                            },
                            "assignee": {
                                "type": "string",
                                "description": "Assignee of the ticket (accountID, full name or e-mail)",
                            },
                            "description": {
                                "type": "string",
                                "description": "Issue description",
                                "default": "",
                            },
                            "additional_fields": {
                                "type": "string",
                                "description": "Optional JSON string of additional fields to set. "
                                "Examples:\n"
                                '- Set priority: {"priority": {"name": "High"}}\n'
                                '- Add labels: {"labels": ["frontend", "urgent"]}\n'
                                '- Add components: {"components": [{"name": "UI"}]}\n'
                                '- Link to parent (for any issue type): {"parent": "PROJ-123"}\n'
                                '- Custom fields: {"customfield_10010": "value"}',
                                "default": "{}",
                            },
                        },
                        "required": ["project_key", "summary", "issue_type"],
                    },
                ),
                Tool(
                    name="jira_create_issue_for_linka",
                    description="Create a new Jira issue for Ambisis Squad named Linka. Making all the necessary standards and following the Ambisis template",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "The project will be specified by the user, but you can use the following prefixes: "
                                "MMP is for melhorias contínuas de produto, como melhorias de usabilidade"
                                "BUG is for bugs, issues, problems, etc."
                                "BIG is for big features, like new screens, new features, etc."
                                "QA is for quality assurance, like automated testing, etc."
                                "If the user does not specify the project, use the default project: MMP",
                            },
                            "summary": {
                                "type": "string",
                                "description": "Summary/title of the issue. In Ambisis we use this template: "
                                "{ Module } -> { optional secondary module / related page } - { the task objective }",

                            },
                            "issue_type": {
                                "type": "string",
                                "description": (
                                    "Issue type (e.g. 'Task', 'Bug'). "
                                ),
                            },
                            "userHistories": {
                                "type": "array",
                                "description": "Lista de Histórias de usuário, você pode colocar 1 ou mais história de usuário, incluindo todos os pontos de vista quando aplicável, exemplo: \"Usuário\", \"Administrador\", \"Analista de dados do Ambisis\", \"Desenvolvedor\", etc. O modelo da história de usuário é bem simples: Eu como [ator] quero que [o que o usuário quer] para que [o motivo do que ele quer]",
                                "default": "[]",
                            },
                            "acceptationCriteria": {
                                "type": "array",
                                "description": "Aqui deve ter uma lista de todos os critérios de aceitação que precisam ser atingidos para a tarefa ser aceita e considerada concluída, essa lista deve servir como base para o dev concluir a tarefa. Considere regras de negócio, detalhes técnicos, testes a se considerar, etc.",
                                "default": "[]",
                            },
                            "taskLocation": {
                                "type": "string",
                                "description": "Aqui deve ser explicado em qual ou quais telas as funcionalidades serão adicionadas, e um breve passo a passo de como chegar na tela",
                                "default": "",
                            },
                            "assignee": {
                                "type": "string",
                                "description": "Assignee of the ticket (accountID, full name or e-mail)",
                            },
                            "additional_fields": {
                                "type": "string",
                                "description": "Optional JSON string of additional fields to set. "
                                "Examples:\n"
                                '- Set priority: {"priority": {"name": "High"}}\n'
                                '- Add labels: {"labels": ["frontend", "urgent"]}\n'
                                '- Always include the following label: "Q1_25" if the task is for the first quarter of 2025, and so on. If the use do not specify the quarter, use the current one.'
                                '- Add components: {"components": [{"name": "UI"}]}\n'
                                '- Link to parent (for any issue type): {"parent": "PROJ-123"}\n'
                                '- Custom fields: {"customfield_10010": "value"}',
                                "default": "{}",
                            },
                        },
                        "required": ["project_key", "summary", "issue_type", "userHistories", "acceptationCriteria", "taskLocation"],
                    },
                ),
                Tool(
                    name="jira_create_idea_for_ambisis",
                    description="Create a new Jira Product Discovery Idea for Ambisis. Making all the necessary standards and following the Ambisis template",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "project_key": {
                                "type": "string",
                                "description": "The project for creating ideas is always RAMB"
                            },
                            "summary": {
                                "type": "string",
                                "description": "Summary/title of the idea. Is literal the idea described in one line"
                            },
                            "briefDescription": {
                                "type": "string",
                                "description": "A simple description of the idea, in one or two sentences. Just for us to know what the idea is about.",
                            },
                            "effort": {
                                "type": "number",
                                "description": "The effort of how complex is to implement the idea, 1 to 5."
                                "1 is the easiest for very simple ideas, like adding a simple field or making a small adjustment in the UI."
                                "2 is for ideas that are a bit complex, like adding a new feature that is not too complex, like adding a new chart logic or a new report that are not too complex."
                                "3 is for ideas that are complex, like adding a new feature that is complex, like adding a new complex report or a new feature that have never been implemented before."
                                "4 is for ideas that are very complex, like adding a new feature that is very complex, like creating a new screen, refactoring a feature that may required multiple changes in multiple projects"
                                "5 is for ideas that are extremely complex, creating entire new modules from scratch."
                            },
                            "impact": {
                                "type": "number",
                                "description": "The impact of the idea, represents how each idea contributes to the goal, 1 to 5."
                                "1 is for ideas that are have little connection to Ambisis goals, like adding a new feature that is not related to the main goal of the project."
                                "2 is for ideas that are have a bit connection to Ambisis goals, something that is not directly related to the main goal of the project, but still contributes to the goal."
                                "3 is for ideas that are have a medium connection to Ambisis goals, something that is related to the main goal of the project, but not directly related."
                                "4 is for ideas that are have a high connection to Ambisis goals, something that is directly related to the main goal of the project."
                                "5 is for ideas that are have a very high connection to Ambisis goals, something that is directly related to the main goal of the project and is a must have."
                            },
                            "value": {
                                "type": "number",
                                "description": "The value perception of the idea for the user or interested agents, a value that an idea will deliver, 1 to 5."
                                "1 is for ideas that will delivery very little value to the user or interested agents, the user will probably not even notice it"
                                "2 is for ideas that will delivery a bit of value to the user or interested agents, the user will not care much about it"
                                "3 is for ideas that will delivery a medium value to the user or interested agents, the user will be happy with it"
                                "4 is for ideas that will delivery a high value to the user or interested agents, the user will be very happy with it"
                                "5 is for ideas that will delivery a very high value to the user or interested agents, the agents will go crazy for it"
                            },
                            "confidence": {
                                "type": "number",
                                "description": "Level of confidence related to the success of an idea, 0 to 100."
                                "0 to 20 is for ideas that are not confident at all, there is lots of uncertainty about the idea"
                                "21 to 40 is for ideas that are somewhat confident, there is a good chance the idea will be successful"
                                "41 to 60 is for ideas that are confident, there is a good chance the idea will be successful"
                                "61 to 80 is for ideas that are very confident, there is a very good chance the idea will be successful"
                                "81 to 100 is for ideas that are extremely confident, there is a very high chance the idea will be successful"
                            },
                            "moduloAmbisis": {
                                "type": "array",
                                "description": 
                                "The modulo of the idea, must be an array of objects with the key id {id: \"10082\"} for example of the Ambisis module that the idea is related to. Possible values:\n"
                                "10082: Relatórios\n"
                                "10083: Impressão PDF\n"
                                "10084: Configurações\n"
                                "10085: Licenças\n"
                                "10086: Checklists\n"
                                "10087: Empresas\n"
                                "10088: Empreendimentos\n"
                                "10089: Gestão de projetos\n"
                                "10090: Gestão de resíduos MTR\n"
                                "10091: Ofícios\n"
                                "10092: Novo módulo\n"
                                "10093: Orçamentos\n"
                                "10094: Usuários\n"
                                "10095: Sistema WEB\n"
                                "10096: Aplicativo mobile\n"
                                "10097: Dashboard inicial\n"
                                "10098: Dashboards\n"
                                "10099: Arquivos\n"
                                "10100: Inteligência artificial\n"
                                "10101: Alertas e prazos\n"
                                "10102: Técnico interno\n"
                                "10103: Ordens de serviço\n"
                                "10104: Atualização automática de licenças\n"
                                "10105: Projetos de licenciamento\n"
                                "10111: Processos minerários\n"
                                "10112: Calendário\n"
                                "10113: Requisitos legais (Legislações)\n"
                                "10114: Protocolos\n"
                                "10115: Certificados\n"
                                "10140: Nova Integração\n"
                                "10141: API Externa\n"
                                "10142: Financeiro",
                            },
                        },
                        "required": ["project_key", "summary", "briefDescription", "effort", "impact", "value", "confidence", "moduloAmbisis"],
                    },
                ),
                Tool(
                    name="jira_update_issue",
                    description="Update an existing Jira issue including changing status, adding Epic links, updating fields, etc.",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                            "fields": {
                                "type": "string",
                                "description": "A valid JSON object of fields to update as a string. "
                                'Example: \'{"summary": "New title", "description": "Updated description", '
                                '"priority": {"name": "High"}, "assignee": {"name": "john.doe"}}\'',
                            },
                            "additional_fields": {
                                "type": "string",
                                "description": "Optional JSON string of additional fields to update. Use this for custom fields or more complex updates.",
                                "default": "{}",
                            },
                        },
                        "required": ["issue_key", "fields"],
                    },
                ),
                Tool(
                    name="jira_delete_issue",
                    description="Delete an existing Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g. PROJ-123)",
                            },
                        },
                        "required": ["issue_key"],
                    },
                ),
                Tool(
                    name="jira_add_comment",
                    description="Add a comment to a Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                            "comment": {
                                "type": "string",
                                "description": "Comment text in Markdown format",
                            },
                        },
                        "required": ["issue_key", "comment"],
                    },
                ),
                Tool(
                    name="jira_add_worklog",
                    description="Add a worklog entry to a Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                            "time_spent": {
                                "type": "string",
                                "description": (
                                    "Time spent in Jira format. Examples: "
                                    "'1h 30m' (1 hour and 30 minutes), "
                                    "'1d' (1 day), '30m' (30 minutes), "
                                    "'4h' (4 hours)"
                                ),
                            },
                            "comment": {
                                "type": "string",
                                "description": "Optional comment for the worklog in Markdown format",
                            },
                            "started": {
                                "type": "string",
                                "description": (
                                    "Optional start time in ISO format. "
                                    "If not provided, the current time will be used. "
                                    "Example: '2023-08-01T12:00:00.000+0000'"
                                ),
                            },
                        },
                        "required": ["issue_key", "time_spent"],
                    },
                ),
                Tool(
                    name="jira_get_worklog",
                    description="Get worklog entries for a Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                        },
                        "required": ["issue_key"],
                    },
                ),
                Tool(
                    name="jira_link_to_epic",
                    description="Link an existing issue to an epic",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "The key of the issue to link (e.g., 'PROJ-123')",
                            },
                            "epic_key": {
                                "type": "string",
                                "description": "The key of the epic to link to (e.g., 'PROJ-456')",
                            },
                        },
                        "required": ["issue_key", "epic_key"],
                    },
                ),
                Tool(
                    name="jira_get_epic_issues",
                    description="Get all issues linked to a specific epic",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "epic_key": {
                                "type": "string",
                                "description": "The key of the epic (e.g., 'PROJ-123')",
                            },
                            "limit": {
                                "type": "number",
                                "description": "Maximum number of issues to return (1-50)",
                                "default": 10,
                                "minimum": 1,
                                "maximum": 50,
                            },
                        },
                        "required": ["epic_key"],
                    },
                ),
                Tool(
                    name="jira_get_transitions",
                    description="Get available status transitions for a Jira issue",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                        },
                        "required": ["issue_key"],
                    },
                ),
                Tool(
                    name="jira_transition_issue",
                    description="Transition a Jira issue to a new status",
                    inputSchema={
                        "type": "object",
                        "properties": {
                            "issue_key": {
                                "type": "string",
                                "description": "Jira issue key (e.g., 'PROJ-123')",
                            },
                            "transition_id": {
                                "type": "string",
                                "description": (
                                    "ID of the transition to perform. Use the jira_get_transitions tool first "
                                    "to get the available transition IDs for the issue. "
                                    "Example values: '11', '21', '31'"
                                ),
                            },
                            "fields": {
                                "type": "string",
                                "description": (
                                    "JSON string of fields to update during the transition. "
                                    "Some transitions require specific fields to be set. "
                                    'Example: \'{"resolution": {"name": "Fixed"}}\''
                                ),
                                "default": "{}",
                            },
                            "comment": {
                                "type": "string",
                                "description": (
                                    "Comment to add during the transition (optional). "
                                    "This will be visible in the issue history."
                                ),
                            },
                        },
                        "required": ["issue_key", "transition_id"],
                    },
                ),
            ]
        )

    return tools


@app.call_tool()
async def call_tool(name: str, arguments: Any) -> Sequence[TextContent]:
    """Handle tool calls for Confluence and Jira operations."""
    ctx = app.request_context.lifespan_context
    try:
        # Helper functions for formatting results
        def format_comment(comment: Any) -> dict:
            if hasattr(comment, "to_simplified_dict"):
                return comment.to_simplified_dict()
            return {
                "id": comment.get("id"),
                "author": comment.get("author", {}).get("displayName", "Unknown"),
                "created": comment.get("created"),
                "body": comment.get("body"),
            }

        # Confluence operations
        if name == "confluence_search":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            query = arguments.get("query", "")
            limit = min(int(arguments.get("limit", 10)), 50)

            # Check if the query is a simple search term or already a CQL query
            if query and not any(
                x in query
                for x in ["=", "~", ">", "<", " AND ", " OR ", "currentUser()"]
            ):
                # Convert simple search term to CQL text search
                # This will search in all content (title, body, etc.)
                query = f'text ~ "{query}"'
                logger.info(f"Converting simple search term to CQL: {query}")

            pages = ctx.confluence.search(query, limit=limit)

            # Format results using the to_simplified_dict method
            search_results = [page.to_simplified_dict() for page in pages]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(search_results, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "confluence_get_page":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            page_id = arguments.get("page_id")
            include_metadata = arguments.get("include_metadata", True)

            page = ctx.confluence.get_page_content(page_id)

            if include_metadata:
                # The to_simplified_dict method already includes the content,
                # so we don't need to include it separately at the root level
                result = {
                    "metadata": page.to_simplified_dict(),
                }
            else:
                # For backward compatibility, keep returning content directly
                result = {"content": page.content}

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "confluence_get_page_children":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            parent_id = arguments.get("parent_id")
            expand = arguments.get("expand", "version")
            limit = min(int(arguments.get("limit", 25)), 50)
            include_content = arguments.get("include_content", False)

            # Add body.storage to expand if content is requested
            if include_content and "body" not in expand:
                expand = f"{expand},body.storage"

            # Get the child pages
            pages = ctx.confluence.get_page_children(
                page_id=parent_id, expand=expand, limit=limit, convert_to_markdown=True
            )

            # Format results using the to_simplified_dict method
            child_pages = [page.to_simplified_dict() for page in pages]

            # Return the formatted results
            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        {
                            "parent_id": parent_id,
                            "total": len(child_pages),
                            "limit": limit,
                            "results": child_pages,
                        },
                        indent=2,
                        ensure_ascii=False,
                    ),
                )
            ]

        elif name == "confluence_get_page_ancestors":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            page_id = arguments.get("page_id")

            # Get the ancestor pages
            ancestors = ctx.confluence.get_page_ancestors(page_id)

            # Format results
            ancestor_pages = [page.to_simplified_dict() for page in ancestors]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(ancestor_pages, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "confluence_get_comments":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            page_id = arguments.get("page_id")
            comments = ctx.confluence.get_page_comments(page_id)

            # Format comments using their to_simplified_dict method if available
            formatted_comments = [format_comment(comment) for comment in comments]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(formatted_comments, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "confluence_create_page":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            # Extract arguments
            space_key = arguments.get("space_key")
            title = arguments.get("title")
            content = arguments.get("content")
            parent_id = arguments.get("parent_id")

            # Create the page (with automatic markdown conversion)
            page = ctx.confluence.create_page(
                space_key=space_key,
                title=title,
                body=content,
                parent_id=parent_id,
                is_markdown=False,
            )

            # Format the result
            result = page.to_simplified_dict()

            return [
                TextContent(
                    type="text",
                    text=f"Page created successfully:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                )
            ]

        elif name == "confluence_update_page":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            page_id = arguments.get("page_id")
            title = arguments.get("title")
            content = arguments.get("content")
            is_minor_edit = arguments.get("is_minor_edit", False)
            version_comment = arguments.get("version_comment", "")

            if not page_id or not title or not content:
                raise ValueError(
                    "Missing required parameters: page_id, title, and content are required."
                )

            # Update the page (with automatic markdown conversion)
            updated_page = ctx.confluence.update_page(
                page_id=page_id,
                title=title,
                body=content,
                is_minor_edit=is_minor_edit,
                version_comment=version_comment,
                is_markdown=False,
            )

            # Format results
            page_data = updated_page.to_simplified_dict()

            return [TextContent(type="text", text=json.dumps({"page": page_data}))]

        elif name == "confluence_delete_page":
            if not ctx or not ctx.confluence:
                raise ValueError("Confluence is not configured.")

            page_id = arguments.get("page_id")

            if not page_id:
                raise ValueError("Missing required parameter: page_id is required.")

            try:
                # Delete the page
                result = ctx.confluence.delete_page(page_id=page_id)

                # Format results - our fixed implementation now correctly returns True on success
                if result:
                    response = {
                        "success": True,
                        "message": f"Page {page_id} deleted successfully",
                    }
                else:
                    # This branch should rarely be hit with our updated implementation
                    # but we keep it for safety
                    response = {
                        "success": False,
                        "message": f"Unable to delete page {page_id}. The API request completed but deletion was unsuccessful.",
                    }

                return [
                    TextContent(
                        type="text",
                        text=json.dumps(response, indent=2, ensure_ascii=False),
                    )
                ]
            except Exception as e:
                # API call failed with an exception
                logger.error(f"Error deleting Confluence page {page_id}: {str(e)}")
                return [
                    TextContent(
                        type="text",
                        text=json.dumps(
                            {
                                "success": False,
                                "message": f"Error deleting page {page_id}",
                                "error": str(e),
                            },
                            indent=2,
                            ensure_ascii=False,
                        ),
                    )
                ]

        # Jira operations
        elif name == "jira_get_issue":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            issue_key = arguments.get("issue_key")
            expand = arguments.get("expand")
            comment_limit = arguments.get("comment_limit")

            issue = ctx.jira.get_issue(
                issue_key, expand=expand, comment_limit=comment_limit
            )

            result = {"content": issue.to_simplified_dict()}

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "jira_search":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            jql = arguments.get("jql")
            fields = arguments.get("fields", "*all")
            limit = min(int(arguments.get("limit", 10)), 50)

            issues = ctx.jira.search_issues(jql, fields=fields, limit=limit)

            # Format results using the to_simplified_dict method
            search_results = [issue.to_simplified_dict() for issue in issues]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(search_results, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "jira_get_project_issues":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            project_key = arguments.get("project_key")
            limit = min(int(arguments.get("limit", 10)), 50)

            issues = ctx.jira.get_project_issues(project_key, limit=limit)

            # Format results
            project_issues = [issue.to_simplified_dict() for issue in issues]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(project_issues, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "jira_create_issue_for_linka":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            # Extract required arguments
            project_key = arguments.get("project_key")
            summary = arguments.get("summary")
            issue_type = arguments.get("issue_type")

            userHistories = arguments.get("userHistories")
            acceptationCriteria = arguments.get("acceptationCriteria")
            taskLocation = arguments.get("taskLocation")

            # Extract optional arguments
            assignee = arguments.get("assignee")

            # Parse additional fields
            additional_fields = {}
            if arguments.get("additional_fields"):
                try:
                    additional_fields = json.loads(arguments.get("additional_fields"))
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in additional_fields")

            # Create the issue
            issue = ctx.jira.create_issue(
                project_key=project_key,
                summary=summary,
                issue_type=issue_type,
                description="h1. DETALHAMENTO:\n"
                f"{"\n".join([f" {{panel:bgColor=#eae6ff}}{userStory}{{panel}}" for userStory in userHistories])}\n"
                "h1. Critérios de aceitação:\n"
                f"{"\n".join([f"{{panel:bgColor=#ffebe6}}{acceptation}{{panel}}" for acceptation in acceptationCriteria])}"
                "\n-----------------------------------------------\n"
                "h1. LOCAL/LUGAR DA TASK:\n"
                f"{{panel:bgColor=#deebff}}{taskLocation}{{panel}}",
                assignee=assignee,
                **additional_fields,
            )

            result = issue.to_simplified_dict()

            return [
                TextContent(
                    type="text",
                    text=f"Issue created successfully:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                )
            ]
            
        elif name == "jira_create_idea_for_ambisis":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")
                
            # Extract required arguments
            project_key = arguments.get("project_key")
            summary = arguments.get("summary")
            brief_description = arguments.get("briefDescription")
            effort = arguments.get("effort")
            impact = arguments.get("impact")
            value = arguments.get("value")
            confidence = arguments.get("confidence")
            moduloAmbisis = arguments.get("moduloAmbisis")
            
            # Create custom fields dictionary
            custom_fields = {
                "customfield_10102": effort,   # Effort custom field
                "customfield_10082": impact,   # Impact custom field
                "customfield_10101": value,    # Value custom field
                "customfield_10104": confidence, # Confidence custom field
                "customfield_10127": moduloAmbisis, # Modulo Ambisis custom field
            }
            
            # Create the issue
            issue = ctx.jira.create_issue(
                project_key=project_key,
                summary=summary,
                issue_type="Idea",
                description=brief_description,
                **custom_fields
            )
            
            result = issue.to_simplified_dict()
            
            return [
                TextContent(
                    type="text",
                    text=f"Idea created successfully:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                )
            ]

        elif name == "jira_create_issue":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            # Extract required arguments
            project_key = arguments.get("project_key")
            summary = arguments.get("summary")
            issue_type = arguments.get("issue_type")

            # Extract optional arguments
            description = arguments.get("description", "")
            assignee = arguments.get("assignee")

            # Parse additional fields
            additional_fields = {}
            if arguments.get("additional_fields"):
                try:
                    additional_fields = json.loads(arguments.get("additional_fields"))
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in additional_fields")

            # Create the issue
            issue = ctx.jira.create_issue(
                project_key=project_key,
                summary=summary,
                issue_type=issue_type,
                description=description,
                assignee=assignee,
                **additional_fields,
            )

            result = issue.to_simplified_dict()

            return [
                TextContent(
                    type="text",
                    text=f"Issue created successfully:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                )
            ]

        elif name == "jira_update_issue":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            # Extract arguments
            issue_key = arguments.get("issue_key")

            # Parse fields JSON
            fields = {}
            if arguments.get("fields"):
                try:
                    fields = json.loads(arguments.get("fields"))
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in fields")

            # Parse additional fields JSON
            additional_fields = {}
            if arguments.get("additional_fields"):
                try:
                    additional_fields = json.loads(arguments.get("additional_fields"))
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in additional_fields")

            try:
                # Update the issue - directly pass fields to JiraFetcher.update_issue
                # instead of using fields as a parameter name
                issue = ctx.jira.update_issue(
                    issue_key=issue_key, **fields, **additional_fields
                )

                result = issue.to_simplified_dict()

                return [
                    TextContent(
                        type="text",
                        text=f"Issue updated successfully:\n{json.dumps(result, indent=2, ensure_ascii=False)}",
                    )
                ]
            except Exception as e:
                return [
                    TextContent(
                        type="text",
                        text=f"Error updating issue {issue_key}: {str(e)}",
                    )
                ]

        elif name == "jira_delete_issue":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            issue_key = arguments.get("issue_key")

            # Delete the issue
            deleted = ctx.jira.delete_issue(issue_key)

            result = {"message": f"Issue {issue_key} has been deleted successfully."}

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "jira_add_comment":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            issue_key = arguments.get("issue_key")
            comment = arguments.get("comment")

            # Add the comment
            result = ctx.jira.add_comment(issue_key, comment)

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "jira_add_worklog":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            # Extract arguments
            issue_key = arguments.get("issue_key")
            time_spent = arguments.get("time_spent")
            comment = arguments.get("comment")
            started = arguments.get("started")

            # Add the worklog
            worklog = ctx.jira.add_worklog(
                issue_key=issue_key,
                time_spent=time_spent,
                comment=comment,
                started=started,
            )

            result = {"message": "Worklog added successfully", "worklog": worklog}

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "jira_get_worklog":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            issue_key = arguments.get("issue_key")

            # Get worklogs
            worklogs = ctx.jira.get_worklogs(issue_key)

            result = {"worklogs": worklogs}

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "jira_link_to_epic":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            issue_key = arguments.get("issue_key")
            epic_key = arguments.get("epic_key")

            # Link the issue to the epic
            issue = ctx.jira.link_issue_to_epic(issue_key, epic_key)

            result = {
                "message": f"Issue {issue_key} has been linked to epic {epic_key}.",
                "issue": issue.to_simplified_dict(),
            }

            return [
                TextContent(
                    type="text", text=json.dumps(result, indent=2, ensure_ascii=False)
                )
            ]

        elif name == "jira_get_epic_issues":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            epic_key = arguments.get("epic_key")
            limit = min(int(arguments.get("limit", 10)), 50)

            # Get issues linked to the epic
            issues = ctx.jira.get_epic_issues(epic_key, limit=limit)

            # Format results
            epic_issues = [issue.to_simplified_dict() for issue in issues]

            return [
                TextContent(
                    type="text",
                    text=json.dumps(epic_issues, indent=2, ensure_ascii=False),
                )
            ]

        elif name == "jira_get_transitions":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            issue_key = arguments.get("issue_key")

            # Get available transitions
            transitions = ctx.jira.get_available_transitions(issue_key)

            # Format transitions
            formatted_transitions = []
            for transition in transitions:
                formatted_transitions.append(
                    {
                        "id": transition.get("id"),
                        "name": transition.get("name"),
                        "to_status": transition.get("to", {}).get("name"),
                    }
                )

            return [
                TextContent(
                    type="text",
                    text=json.dumps(
                        formatted_transitions, indent=2, ensure_ascii=False
                    ),
                )
            ]

        elif name == "jira_transition_issue":
            if not ctx or not ctx.jira:
                raise ValueError("Jira is not configured.")

            # Extract arguments
            issue_key = arguments.get("issue_key")
            transition_id = arguments.get("transition_id")
            comment = arguments.get("comment")

            # Validate required parameters
            if not issue_key:
                raise ValueError("issue_key is required")
            if not transition_id:
                raise ValueError("transition_id is required")

            # Convert transition_id to integer if it's a numeric string
            # This ensures compatibility with the Jira API which expects integers
            if isinstance(transition_id, str) and transition_id.isdigit():
                transition_id = int(transition_id)
                logger.debug(
                    f"Converted string transition_id to integer: {transition_id}"
                )

            # Parse fields JSON
            fields = {}
            if arguments.get("fields"):
                try:
                    fields = json.loads(arguments.get("fields"))
                except json.JSONDecodeError:
                    raise ValueError("Invalid JSON in fields")

            try:
                # Transition the issue
                issue = ctx.jira.transition_issue(
                    issue_key=issue_key,
                    transition_id=transition_id,
                    fields=fields,
                    comment=comment,
                )

                result = {
                    "message": f"Issue {issue_key} transitioned successfully",
                    "issue": issue.to_simplified_dict() if issue else None,
                }

                return [
                    TextContent(
                        type="text",
                        text=json.dumps(result, indent=2, ensure_ascii=False),
                    )
                ]
            except Exception as e:
                # Provide a clear error message, especially for transition ID type issues
                error_msg = str(e)
                if "'transition' identifier must be an integer" in error_msg:
                    error_msg = (
                        f"Error transitioning issue {issue_key}: The Jira API requires transition IDs to be integers. "
                        f"Received transition ID '{transition_id}' of type {type(transition_id).__name__}. "
                        f"Please use the numeric ID value from jira_get_transitions."
                    )
                else:
                    error_msg = f"Error transitioning issue {issue_key} with transition ID {transition_id}: {error_msg}"

                logger.error(error_msg)
                return [
                    TextContent(
                        type="text",
                        text=error_msg,
                    )
                ]

        raise ValueError(f"Unknown tool: {name}")

    except Exception as e:
        logger.error(f"Tool execution error: {str(e)}")
        return [TextContent(type="text", text=f"Error: {str(e)}")]


async def run_server(transport: str = "stdio", port: int = 8000) -> None:
    """Run the MCP Atlassian server with the specified transport."""
    if transport == "sse":
        from mcp.server.sse import SseServerTransport
        from starlette.applications import Starlette
        from starlette.requests import Request
        from starlette.routing import Mount, Route

        sse = SseServerTransport("/messages/")

        async def handle_sse(request: Request) -> None:
            async with sse.connect_sse(
                request.scope, request.receive, request._send
            ) as streams:
                await app.run(
                    streams[0], streams[1], app.create_initialization_options()
                )

        starlette_app = Starlette(
            debug=True,
            routes=[
                Route("/sse", endpoint=handle_sse),
                Mount("/messages/", app=sse.handle_post_message),
            ],
        )

        import uvicorn

        # Set up uvicorn config
        config = uvicorn.Config(starlette_app, host="0.0.0.0", port=port)  # noqa: S104
        server = uvicorn.Server(config)
        # Use server.serve() instead of run() to stay in the same event loop
        await server.serve()
    else:
        from mcp.server.stdio import stdio_server

        async with stdio_server() as (read_stream, write_stream):
            await app.run(
                read_stream, write_stream, app.create_initialization_options()
            )
