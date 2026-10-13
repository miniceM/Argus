"""只拉取 Compose 声明的固定 digest；不需要凭据，不启动服务或读取 .env。"""

import re
import subprocess
from pathlib import Path

from local_runtime import client_environment, require_local_docker


def main():
    env = client_environment()
    require_local_docker(env)
    source = (Path(__file__).parent / "compose.yaml").read_text()
    images = re.findall(r"^    image: (\S+)$", source, re.MULTILINE)
    if len(images) != 6 or any(not re.search(r"@sha256:[0-9a-f]{64}$", image) for image in images):
        raise ValueError("Every S4 fixture image must have an immutable digest")
    for image in images:
        subprocess.run(["docker", "pull", image], check=True, env=env)


if __name__ == "__main__":
    main()
