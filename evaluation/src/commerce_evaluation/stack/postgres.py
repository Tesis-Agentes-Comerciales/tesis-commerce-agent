"""The disposable PostgreSQL of a live stack: the Merchant CI image in a new container."""

import os
import secrets
import subprocess

from commerce_evaluation.stack.environment import free_port, wait_until

# Same image and digest as the Merchant CI and the joint E2E workflow.
POSTGRES_IMAGE = (
    "postgres:18.4-alpine@sha256:9a8afca54e7861fd90fab5fdf4c42477a6b1cb7d293595148e674e0a3181de15"
)
# The image's PGDATA; the Merchant D17 suites refuse any other data_directory.
POSTGRES_DATA = "/var/lib/postgresql/18/docker"
CONTAINER_PREFIX = "tesis-live-"


class PostgresContainer:
    """The CI image in a new container; the Merchant's role owns tesis_lab and nothing else."""

    def __init__(self):
        self.name = CONTAINER_PREFIX + secrets.token_hex(6)
        self.port = free_port()
        self.app_user = "live_app"
        self._admin_password = secrets.token_hex(20)
        self._app_password = secrets.token_hex(20)
        self.created = False
        self.running = False

    @property
    def dsn(self) -> str:
        return f"postgresql://{self.app_user}:{self._app_password}@127.0.0.1:{self.port}/tesis_lab"

    def docker(self, *args: str, input: str | None = None, timeout: float = 120) -> str:
        result = subprocess.run(
            ["docker", *args],
            input=input,
            capture_output=True,
            text=True,
            timeout=timeout,
            # Passed by name with --env, so the password never appears in an argument list.
            env={**os.environ, "POSTGRES_PASSWORD": self._admin_password},
        )
        if result.returncode:
            # Do not echo commands, passwords or server output.
            raise RuntimeError(f"LIVE_STACK_DOCKER_FAILED:{args[0]}:{result.returncode}")
        return result.stdout.strip()

    def start(self) -> None:
        self.docker(
            "run",
            "--detach",
            "--name",
            self.name,
            "--label",
            "tesis-live=1",
            "--env",
            "POSTGRES_USER=live_admin",
            "--env",
            "POSTGRES_PASSWORD",
            "--env",
            "POSTGRES_DB=postgres",
            "--env",
            f"PGDATA={POSTGRES_DATA}",
            "--publish",
            f"127.0.0.1:{self.port}:5432",
            POSTGRES_IMAGE,
            # The unique name lets every fixture connection prove it reached this cluster.
            "-c",
            f"cluster_name={self.name}",
            "-c",
            "timezone=UTC",
        )
        self.created = self.running = True
        self.wait_ready()
        self.admin_sql(
            f"CREATE ROLE {self.app_user} LOGIN PASSWORD '{self._app_password}' "
            "NOSUPERUSER NOCREATEDB NOCREATEROLE;\n"
            f"CREATE DATABASE tesis_lab OWNER {self.app_user};\n"
            # Only for the data_directory identity checks of the Merchant's own suites.
            f"GRANT pg_read_all_settings TO {self.app_user};\n"
        )

    def wait_ready(self) -> None:
        # TCP inside the container: the image's init server listens on its socket only.
        wait_until(
            lambda: self.docker(
                "exec", self.name, "pg_isready", "-q", "-h", "127.0.0.1", "-U", "live_admin"
            ),
            "postgres",
        )

    def admin_sql(self, statements: str) -> str:
        # SQL travels on stdin; the container's local socket trusts its own superuser.
        return self.docker(
            "exec",
            "--interactive",
            self.name,
            "psql",
            "-X",
            "-qAt",
            "-v",
            "ON_ERROR_STOP=1",
            "-U",
            "live_admin",
            "-d",
            "postgres",
            input=statements,
        )

    def stop(self) -> None:
        self.docker("stop", "--time", "10", self.name)
        self.running = False

    def restart(self) -> None:
        self.docker("start", self.name)
        self.running = True
        self.wait_ready()

    def remove(self) -> None:
        if self.created:
            self.docker("rm", "--force", "--volumes", self.name)
            self.created = self.running = False
