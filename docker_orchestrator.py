import time
import uuid
import shutil
from functools import wraps

import docker
from docker import errors as docker_errors
from pathlib import Path
from typing import Optional

from docker.models.containers import Container

def require_container(func):
    @wraps(func)
    def wrapper(self, *args, **kwargs):
        if not self.container:
            return { "error": "No container running. Start the container first" }
        try:
            return func(self, *args, **kwargs)
        except Exception as e:
            return { "error": str(e) }

    return wrapper


class DockerOrchestrator:
    def __init__(self, run_id: str, workspace_root: Path):
        self.run_id = run_id
        self.workspace_root = workspace_root
        self.project_dir = workspace_root / run_id
        self.project_dir.mkdir(parents=True, exist_ok=True)
        self.client = docker.from_env()
        self.container: Optional[Container] = None
        self.initial_setup()

    def initial_setup(self):
        try:
            self.client.images.get('node:22-alpine')
        except docker_errors.NotFound:
            self.client.images.create('node:22-alpine')

    def get_container(self):

        try:
            if self.client.containers.get(self.run_id): return self.restart_previous_container()
        except docker_errors.NotFound:
            return self.create_new_container()

        except Exception as e:
            print(e)
            return {
                "error": str(e)
            }


    def create_new_container(self):
        try:
            container = self.client.containers.run(
                image='node:22-alpine',
                name=self.run_id,
                detach=True,
                volumes={str(self.project_dir): {'bind': '/app', 'mode': 'rw'}},
                working_dir='/app',
                ports={'5173/tcp': None},
                command="tail -f /dev/null"
            )
            self.container = container
            self._run_command_strict("apk add --no-cache git")
            self._run_command_strict("npx -y degit  OMGATE23/vite-react-ts-tailwind-router-template . --force")
            self._run_command_strict("npm install")


            return self._ensure_dev_server_and_return_info()
        except Exception as e:
            return {
                "error": str(e)
            }

    def restart_previous_container(self):
        try:
            container = self.client.containers.get(container_id=self.run_id)
            if container.status != "running":
                container.start()
                time.sleep(2)

            exit_code, output = container.exec_run("pgrep -f 'vite'")
            if exit_code != 0:
                print("Dev server not running. Starting it...")
                container.exec_run("npm run dev -- --host", detach=True)
                time.sleep(2)
            ports = container.attrs["NetworkSettings"]["Ports"]
            host_port = ports["5173/tcp"][0]["HostPort"]
            self.container = container
            return {
                "name": container.name,
                "url": f"http://localhost:{host_port}"
            }
        except Exception as e:
            return {
                "error" : str(e)
            }

    def _run_command_strict(self, command: str):
        exit_code, output = self.container.exec_run(command)
        if exit_code != 0:
            raise Exception(f"Command failed: {command}\n Output: {output.decode('utf-8')}")

    def _ensure_dev_server_and_return_info(self):
        exit_code, _ = self.container.exec_run("pgrep -f 'vite'")

        if exit_code != 0:
            self.container.exec_run("npm run dev -- --host", detach=True)
            time.sleep(2)

        self.container.reload()
        ports = self.container.attrs["NetworkSettings"]["Ports"]
        host_port = ports["5173/tcp"][0]["HostPort"]

        return {
            "name": self.container.name,
            "url": f"http://localhost:{host_port}"
        }

    def _get_safe_path(self, path_str: str) -> Path:
        target_path = (self.project_dir / path_str).resolve()
        if not str(target_path).startswith((str(self.project_dir.resolve()))):
            raise PermissionError("Access Denied: Path outside workspace")

        return target_path

    @require_container
    def list_files(self, subpath: str = "."):
        target_path = self._get_safe_path(subpath)

        if not target_path.exists():
            return {"error": "Path does not exist"}

        items = []
        for item in target_path.iterdir():
            items.append({
                "name": item.name,
                "type": "folder" if item.is_dir() else "file",
                "path": str(item.relative_to(self.project_dir))
            })
        return sorted(items, key=lambda x: (x['type'] != 'folder', x['name']))

    @require_container
    def read_file(self, file_path: str):
        target_path = self._get_safe_path(file_path)

        if not target_path.is_file():
            return {"error": "File not found or is a directory"}

        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()
        return {"content": content}

    @require_container
    def read_file_formatted(self, file_path: str):
        """
        Reads a file and returns it in a Markdown table format with line numbers.
        Format:
        | Line | Content |
        | 1    | import os |
        """
        target_path = self._get_safe_path(file_path)

        if not target_path.is_file():
            return {"error": "File not found or is a directory"}

        with open(target_path, "r", encoding="utf-8") as f:
            lines = f.readlines()

        # Build Markdown Table
        table = "| Line | Code |\n|---:|:---|\n"

        for idx, line in enumerate(lines, start=1):
            safe_code = line.rstrip().replace("|", "\\|")
            table += f"| {idx} | {safe_code} |\n"

        return {"content": table}
    @require_container
    def write_file_folder(self, path: str, type_of_content: str, content: str = None):

        target_path = self._get_safe_path(path)

        if type_of_content == "folder":
            target_path.mkdir(parents=True, exist_ok=True)
            return {"status": "success", "message": f"Folder {path} created"}

        elif type_of_content == "file":
            # Ensure parent directory exists
            target_path.parent.mkdir(parents=True, exist_ok=True)
            with open(target_path, "w", encoding="utf-8") as f:
                f.write(content if content else "")
            return {"status": "success", "message": f"File {path} created"}

        else:
            return {"error": "Invalid type. Must be 'file' or 'folder'"}


    def delete_item(self, path: str):
        cmd = f"rm -rf {path}"
        exit_code, output = self.container.exec_run(cmd, workdir="/app")

        if exit_code == 0:
            return {"status": "success", "message": f"Deleted {path}"}
        else:
            return {"error": output.decode("utf-8")}

    @require_container
    def search_content(self, search_term: str):
        cmd = f'grep -r -n -I "{search_term}" .'
        exit_code, output = self.container.exec_run(cmd, workdir="/app")

        result = output.decode("utf-8")
        if exit_code != 0 and not result:
            return []

        matches = []
        for line in result.strip().split('\n'):
            if line:
                parts = line.split(':', 2)  # split file:line:content
                if len(parts) >= 3:
                    matches.append({
                        "file": parts[0],
                        "line": parts[1],
                        "content": parts[2].strip()
                    })
        return matches

    @require_container
    def replace_code(self, file_path: str, search_block: str, replace_block: str):
        """
        Replaces exact code block.
        NOTE: search_block must match EXACTLY (whitespace, newlines).
        """
        target_path = self._get_safe_path(file_path)

        if not target_path.is_file():
            return {"error": "File not found"}

        with open(target_path, "r", encoding="utf-8") as f:
            content = f.read()

        # Safety Check: Ensure unique match
        count = content.count(search_block)
        if count == 0:
            return {"error": "Search block not found. Ensure whitespace/indentation matches exactly."}
        if count > 1:
            return {
                "error": f"Ambiguous match: Block found {count} times. Include more surrounding lines in search_block."}

        updated_content = content.replace(search_block, replace_block, 1)

        with open(target_path, "w", encoding="utf-8") as f:
            f.write(updated_content)

        return {"status": "success", "message": "Code block replaced successfully."}

    @require_container
    def move_item(self, source_path: str, destination_path: str):
        """
        Move or Rename files and folders.
        Ex: move_item("src/utils/old.ts", "src/utils/new.ts")
        """
        src = self._get_safe_path(source_path)

        dest = (self.project_dir / destination_path).resolve()
        if not str(dest).startswith(str(self.project_dir.resolve())):
            return {"error": "Access denied: Destination outside workspace"}

        if not src.exists():
            return {"error": "Source file not found"}

        if dest.exists():
            return {"error": "Destination already exists"}

        # Ensure dest parent folder exists
        dest.parent.mkdir(parents=True, exist_ok=True)

        shutil.move(str(src), str(dest))
        return {"status": "success", "message": f"Moved {source_path} to {destination_path}"}

    @require_container
    def run_command(self, command: str):
        exit_code, output = self.container.exec_run(command, workdir="/app")
        return {
            "exit_code": exit_code,
            "output": output.decode("utf-8")
        }

    @require_container
    def check_lint_errors(self):
        exit_code, output = self.container.exec_run(cmd="npx tsc --noEmit", workdir="/app")

        return {
            "exit_code": exit_code,
            "output": output
        }

    @require_container
    def get_server_logs(self, lines: int = 50):
        try:
            log_output = self.container.logs(tail=lines).decode("utf-8")
            return {"logs": log_output}
        except Exception as e:
            return {"error": str(e)}


if __name__ == "__main__":
    input_run_id = input("Please give a run ID: ")
    workspace_root_path = Path(__file__).parent.resolve() / "workspace"
    orchestrator = DockerOrchestrator(
        run_id=input_run_id if input_run_id != "" else str(uuid.uuid4()),
        workspace_root=workspace_root_path
    )

    orchestrator.get_container()

    print(orchestrator.container.exec_run("pgrep -f 'vite'"))