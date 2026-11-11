"""Public entry point. Importing this module performs no I/O."""


async def run_task(task, *, ports, settings, provider=None, registry=None):
    from agent.runner import run_task as execute

    return await execute(task, ports=ports, settings=settings, provider=provider, registry=registry)
