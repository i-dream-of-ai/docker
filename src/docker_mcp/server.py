import asyncio
import signal
import sys
from typing import List, Dict, Any, Optional
import mcp.types as types
from mcp.server import NotificationOptions, Server, RequestContext
from mcp.server.models import InitializationOptions
import mcp.server.stdio
from handlers import DockerHandlers


server = Server("docker-mcp")


@server.list_prompts()
async def handle_list_prompts() -> List[types.Prompt]:
    return [
        types.Prompt(
            name="deploy-stack",
            description="Generate and deploy a Docker stack based on requirements",
            arguments=[
                types.PromptArgument(
                    name="requirements",
                    description="Description of the desired Docker stack",
                    required=True
                ),
                types.PromptArgument(
                    name="project_name",
                    description="Name for the Docker Compose project",
                    required=True
                )
            ]
        )
    ]


@server.get_prompt()
async def handle_get_prompt(name: str, arguments: Dict[str, str] | None) -> types.GetPromptResult:
    if name != "deploy-stack":
        raise ValueError(f"Unknown prompt: {name}")

    if not arguments or "requirements" not in arguments or "project_name" not in arguments:
        raise ValueError("Missing required arguments")

    system_message = (
        "You are a Docker deployment specialist. Generate appropriate Docker Compose YAML or "
        "container configurations based on user requirements. For simple single-container "
        "deployments, use the create-container tool. For multi-container deployments, generate "
        "a docker-compose.yml and use the deploy-compose tool. To access logs, first use the "
        "list-containers tool to discover running containers, then use the get-logs tool to "
        "retrieve logs for a specific container."
    )

    user_message = f"""Please help me deploy the following stack:
Requirements: {arguments['requirements']}
Project name: {arguments['project_name']}

Analyze if this needs a single container or multiple containers. Then:
1. For single container: Use the create-container tool with format:
{{
    "image": "image-name",
    "name": "container-name",
    "ports": {{"80": "80"}},
    "environment": {{"ENV_VAR": "value"}}
}}

2. For multiple containers: Use the deploy-compose tool with format:
{{
    "project_name": "example-stack",
    "compose_yaml": "version: '3.8'\\nservices:\\n  service1:\\n    image: image1:latest\\n    ports:\\n      - '8080:80'"
}}"""

    return types.GetPromptResult(
        description="Generate and deploy a Docker stack",
        messages=[
            types.PromptMessage(
                role="system",
                content=types.TextContent(
                    type="text",
                    text=system_message
                )
            ),
            types.PromptMessage(
                role="user",
                content=types.TextContent(
                    type="text",
                    text=user_message
                )
            )
        ]
    )


@server.list_tools()
async def handle_list_tools() -> List[types.Tool]:
    return [
        types.Tool(
            name="create-container",
            description="Create a new standalone Docker container",
            inputSchema={
                "type": "object",
                "properties": {
                    "image": {"type": "string"},
                    "name": {"type": "string"},
                    "ports": {
                        "type": "object",
                        "additionalProperties": {"type": "string"}
                    },
                    "environment": {
                        "type": "object",
                        "additionalProperties": {"type": "string"}
                    }
                },
                "required": ["image"]
            }
        ),
        types.Tool(
            name="deploy-compose",
            description="Deploy a Docker Compose stack",
            inputSchema={
                "type": "object",
                "properties": {
                    "compose_yaml": {"type": "string"},
                    "project_name": {"type": "string"}
                },
                "required": ["compose_yaml", "project_name"]
            }
        ),
        types.Tool(
            name="get-logs",
            description="Retrieve the latest logs for a specified Docker container",
            inputSchema={
                "type": "object",
                "properties": {
                    "container_name": {"type": "string"}
                },
                "required": ["container_name"]
            }
        ),
        types.Tool(
            name="list-containers",
            description="List all Docker containers",
            inputSchema={
                "type": "object",
                "properties": {}
            }
        )
    ]


@server.call_tool()
async def handle_call_tool(name: str, arguments: Dict[str, Any] | None) -> List[types.TextContent]:
    if not arguments and name != "list-containers":
        raise ValueError("Missing arguments")


    ctx = server.request_context
    progress_token = ctx.meta.progress_token if ctx and ctx.meta else None
    
    try:
        if progress_token:
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=0,
                total=1.0
            )
        result = None
        if name == "create-container":
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.3,
                    message="Creating Containers...",
                    error="Container creation has failed, check logs...",
                    total=1.0
                )
            result = await DockerHandlers.handle_create_container(arguments, ctx)
        elif name == "deploy-compose":
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.3,
                    message="Docker compose deployment is in progress...",
                    error="Deployment failed, check logs...",
                    total=1.0
                )
            result = await DockerHandlers.handle_deploy_compose(arguments, ctx)
        elif name == "get-logs":
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.3,
                    message="Getting logs...",
                    error="Logs fetching failed...",
                    total=1.0
                )
            result = await DockerHandlers.handle_get_logs(arguments, ctx)
        elif name == "list-containers":
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.3,
                    message="Listing containers...",
                    error="Error listing containers...",
                    total=1.0
                )
            result = await DockerHandlers.handle_list_containers(arguments, ctx)
        else:
            raise ValueError(f"Unknown tool: {name}")
        if progress_token:
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=1.0,
                message="Checking server...",
                error="Error running this server...",
                total=1.0
            )
        return result
    except Exception as e:
        if progress_token:
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=1.0,
                total=1.0,
                message="Error completing operations...",
                error=str(e)
            )
        return [types.TextContent(type="text", text=f"Error: {str(e)} | Arguments: {arguments}")]


async def main():
    signal.signal(signal.SIGINT, handle_shutdown)
    signal.signal(signal.SIGTERM, handle_shutdown)

    async with mcp.server.stdio.stdio_server() as (read_stream, write_stream):
        await server.run(
            read_stream,
            write_stream,
            InitializationOptions(
                server_name="docker-mcp",
                server_version="0.1.0",
                capabilities=server.get_capabilities(
                    notification_options=NotificationOptions(),
                    experimental_capabilities={},
                ),
            ),
        )


def handle_shutdown(signum, frame):
    print("Shutting down gracefully...")
    sys.exit(0)


if __name__ == "__main__":
    asyncio.run(main())
