from typing import List, Dict, Any, Optional
import asyncio
import os
import yaml
import platform
from python_on_whales import DockerClient
from mcp.types import TextContent, Tool, Prompt, PromptArgument, GetPromptResult, PromptMessage
from docker_executor import DockerComposeExecutor
from mcp.server import RequestContext

docker_client = DockerClient()


async def parse_port_mapping(host_key: str, container_port: str | int) -> tuple[str, str] | tuple[str, str, str]:
    if '/' in str(host_key):
        host_port, protocol = host_key.split('/')
        if protocol.lower() == 'udp':
            return (str(host_port), str(container_port), 'udp')
        return (str(host_port), str(container_port))

    if isinstance(container_port, str) and '/' in container_port:
        port, protocol = container_port.split('/')
        if protocol.lower() == 'udp':
            return (str(host_key), port, 'udp')
        return (str(host_key), port)

    return (str(host_key), str(container_port))


class DockerHandlers:
    TIMEOUT_AMOUNT = 200

    @staticmethod
    async def handle_create_container(arguments: Dict[str, Any], ctx: Optional[RequestContext] = None) -> List[TextContent]:      
        progress_token = ctx.meta.progress_token if ctx and ctx.meta else None    
        try:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.1,
                    total=1.0,
                    message="Validating container configuration..."
                )
                
            image = arguments["image"]
            container_name = arguments.get("name")
            ports = arguments.get("ports", {})
            environment = arguments.get("environment", {})

            if not image:
                raise ValueError("Image name cannot be empty")

            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.2,
                    total=1.0,
                    message="Processing port mappings..."
                )
            port_mappings = []
            for host_key, container_port in ports.items():
                mapping = await parse_port_mapping(host_key, container_port)
                port_mappings.append(mapping)

            async def pull_and_run():
                if not docker_client.image.exists(image):
                    if progress_token:
                        await ctx.session.send_progress_notification(
                            progress_token=progress_token,
                            progress=0.4,
                            total=1.0,
                            message=f"Pulling image {image}..."
                        )
                    await asyncio.to_thread(docker_client.image.pull, image)
                if progress_token:
                    await ctx.session.send_progress_notification(
                        progress_token=progress_token,
                        progress=0.7,
                        total=1.0,
                        message="Creating and starting container..."
                    )
                container = await asyncio.to_thread(
                    docker_client.container.run,
                    image,
                    name=container_name,
                    publish=port_mappings,
                    envs=environment,
                    detach=True
                )
                return container

            container = await asyncio.wait_for(pull_and_run(), timeout=DockerHandlers.TIMEOUT_AMOUNT)
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=1.0,
                    total=1.0,
                    message="Container created successfully"
                )
            return [TextContent(type="text", text=f"Created container '{container.name}' (ID: {container.id})")]
        except asyncio.TimeoutError:
            return [TextContent(type="text", text=f"Operation timed out after {DockerHandlers.TIMEOUT_AMOUNT} seconds")]
        except Exception as e:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=1.0,
                    total=1.0,
                    error=str(e)
                )
            return [TextContent(type="text", text=f"Error creating container: {str(e)} | Arguments: {arguments}")]

    @staticmethod
    async def handle_deploy_compose(arguments: Dict[str, Any], ctx: Optional[RequestContext] = None) -> List[TextContent]:
        debug_info = []
        progress_token = ctx.meta.progress_token if ctx and ctx.meta else None
        
        try:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.1,
                    total=1.0,
                    message="Validating compose configuration..."
                )
                
            compose_yaml = arguments.get("compose_yaml")
            project_name = arguments.get("project_name")

            if not compose_yaml or not project_name:
                raise ValueError(
                    "Missing required compose_yaml or project_name")
            
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.2,
                    total=1.0,
                    message="Processing YAML configuration..."
                )
            yaml_content = DockerHandlers._process_yaml(compose_yaml, debug_info)
            
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.3,
                    total=1.0,
                    message="Saving docker-compose file..."
                )
            compose_path = DockerHandlers._save_compose_file(yaml_content, project_name)

            try:
                if progress_token:
                    await ctx.session.send_progress_notification(
                        progress_token=progress_token,
                        progress=0.4,
                        total=1.0,
                        message="Starting deployment..."
                    )
                result = await DockerHandlers._deploy_stack(compose_path, project_name, debug_info, ctx)
                
                if progress_token:
                    await ctx.session.send_progress_notification(
                        progress_token=progress_token,
                        progress=1.0,
                        total=1.0,
                        message="Deployment completed successfully"
                    )
                return [TextContent(type="text", text=result)]
            finally:
                DockerHandlers._cleanup_files(compose_path)

        except Exception as e:
            debug_output = "\n".join(debug_info)
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=1.0,
                    total=1.0,
                    error=str(e)
                )
            return [TextContent(type="text", text=f"Error deploying compose stack: {str(e)}\n\nDebug Information:\n{debug_output}")]

    @staticmethod
    def _process_yaml(compose_yaml: str, debug_info: List[str]) -> dict:
        debug_info.append("=== Original YAML ===")
        debug_info.append(compose_yaml)

        try:
            yaml_content = yaml.safe_load(compose_yaml)
            debug_info.append("\n=== Loaded YAML Structure ===")
            debug_info.append(str(yaml_content))
            return yaml_content
        except yaml.YAMLError as e:
            raise ValueError(f"Invalid YAML format: {str(e)}")

    @staticmethod
    def _save_compose_file(yaml_content: dict, project_name: str) -> str:
        compose_dir = os.path.join(os.getcwd(), "docker_compose_files")
        os.makedirs(compose_dir, exist_ok=True)

        compose_yaml = yaml.safe_dump(
            yaml_content, default_flow_style=False, sort_keys=False)
        compose_path = os.path.join(
            compose_dir, f"{project_name}-docker-compose.yml")

        with open(compose_path, 'w', encoding='utf-8') as f:
            f.write(compose_yaml)
            f.flush()
            if platform.system() != 'Windows':
                os.fsync(f.fileno())

        return compose_path

    @staticmethod
    async def _deploy_stack(compose_path: str, project_name: str, debug_info: List[str], ctx: Optional[RequestContext] = None) -> str:
        progress_token = ctx.meta.progress_token if ctx and ctx.meta else None
        compose = DockerComposeExecutor(compose_path, project_name)

        for i, command in enumerate([compose.down, compose.up]):
            try:
                if progress_token:
                    message = "Stopping existing services..." if i == 0 else "Starting services..."
                    progress = 0.5 if i == 0 else 0.7
                    await ctx.session.send_progress_notification(
                        progress_token=progress_token,
                        progress=progress,
                        total=1.0,
                        message=message
                    )
                code, out, err = await command()
                debug_info.extend([
                    f"\n=== {command.__name__.capitalize()} Command ===",
                    f"Return Code: {code}",
                    f"Stdout: {out}",
                    f"Stderr: {err}"
                ])

                if code != 0 and command == compose.up:
                    raise Exception(f"Deploy failed with code {code}: {err}")
            except Exception as e:
                if command != compose.down:
                    raise e
                debug_info.append(f"Warning during {command.__name__}: {str(e)}")
        if progress_token:
            await ctx.session.send_progress_notification(
                progress_token=progress_token,
                progress=0.9,
                total=1.0,
                message="Checking service status..."
            )
        code, out, err = await compose.ps()
        service_info = out if code == 0 else "Unable to list services"

        return (f"Successfully deployed compose stack '{project_name}'\n"
                f"Running services:\n{service_info}\n\n"
                f"Debug Info:\n{chr(10).join(debug_info)}")

    @staticmethod
    def _cleanup_files(compose_path: str) -> None:
        try:
            if os.path.exists(compose_path):
                os.remove(compose_path)
            compose_dir = os.path.dirname(compose_path)
            if os.path.exists(compose_dir) and not os.listdir(compose_dir):
                os.rmdir(compose_dir)
        except Exception as e:
            print(f"Warning during cleanup: {str(e)}")

    @staticmethod
    async def handle_get_logs(arguments: Dict[str, Any], ctx: Optional[RequestContext] = None) -> List[TextContent]:
        debug_info = []
        progress_token = ctx.meta.progress_token if ctx and ctx.meta else None
        
        try:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.2,
                    total=1.0,
                    message="Validating container name..."
                )
            container_name = arguments.get("container_name")
            if not container_name:
                raise ValueError("Missing required container_name")

            debug_info.append(f"Fetching logs for container '{container_name}'")
            
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.5,
                    total=1.0,
                    message=f"Fetching logs for container'{container_name}'..."
                )
            logs = await asyncio.to_thread(docker_client.container.logs, container_name, tail=100)

            return [TextContent(type="text", text=f"Logs for container '{container_name}':\n{logs}\n\nDebug Info:\n{chr(10).join(debug_info)}")]
        except Exception as e:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=1.0,
                    total=1.0,
                    error=str(e)
                )
            debug_output = "\n".join(debug_info)
            return [TextContent(type="text", text=f"Error retrieving logs: {str(e)}\n\nDebug Information:\n{debug_output}")]

    @staticmethod
    async def handle_list_containers(arguments: Dict[str, Any], ctx: Optional[RequestContext] = None) -> List[TextContent]:
        debug_info = []
        progress_token = ctx.meta.progress_token if ctx and ctx.meta else None

        try:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.3,
                    total=1.0,
                    message="Fetching container list..."
                )
                
            debug_info.append("Listing all Docker containers")
            containers = await asyncio.to_thread(docker_client.container.list, all=True)
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=0.7,
                    total=1.0,
                    message="Processing container information..."
                )
                
            container_list = "\n".join(
                [f"{c.id[:12]} - {c.name} - {c.state.status}" for c in containers])

            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=1.0,
                    total=1.0,
                    message="Container list retrieved successfully"
                )
            return [TextContent(type="text", text=f"All Docker Containers:\n{container_list}\n\nDebug Info:\n{chr(10).join(debug_info)}")]
        except Exception as e:
            if progress_token:
                await ctx.session.send_progress_notification(
                    progress_token=progress_token,
                    progress=1.0,
                    total=1.0,
                    error=str(e)
                )
            debug_output = "\n".join(debug_info)
            return [TextContent(type="text", text=f"Error listing containers: {str(e)}\n\nDebug Information:\n{debug_output}")]
