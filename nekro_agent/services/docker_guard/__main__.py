"""`python -m nekro_agent.services.docker_guard` 启动 guard 代理。"""

from nekro_agent.services.docker_guard.proxy import run

if __name__ == "__main__":
    run()
