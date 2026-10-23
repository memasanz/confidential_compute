"""On-premises Agent Framework caller: Foundry inference, confidential MCP execution."""

import asyncio
import csv
import math
from typing import Annotated

from agent_framework import Agent
from agent_framework.foundry import FoundryChatClient
from azure.ai.projects.aio import AIProjectClient
from azure.identity.aio import DefaultAzureCredential

from pool_client import EXAMPLES, PoolClient, PoolConfig, required_setting

PROMPT = """Analyze the synthetic input.xlsx already in your confidential workspace.
Use pandas to summarize all numeric columns with count, mean, and sum, rounded
to two decimals. Save summary.csv with columns metric,revenue,units and exactly
the metric rows count,mean,sum. Print the small summary and explain it briefly.
Do not access the network, install packages, or print the original spreadsheet.
"""


def session_tools(pool, session_id):
    async def run_python(code: Annotated[str, "Python code to run in the confidential workspace"]) -> str:
        """Execute Python in this analysis session; pandas and openpyxl are installed."""
        return await pool.call("run_python", session_id=session_id, code=code, timeout=120)

    async def list_files() -> str:
        """List files in this analysis session's workspace."""
        return await pool.call("list_dir", session_id=session_id, path=".")

    return [run_python, list_files]


def validate_summary(path):
    with path.open(newline="", encoding="utf-8") as source:
        reader = csv.DictReader(source)
        if reader.fieldnames != ["metric", "revenue", "units"]:
            raise ValueError("Expected summary.csv columns: metric,revenue,units")
        rows = list(reader)
    if len(rows) != 3 or {row["metric"] for row in rows} != {"count", "mean", "sum"}:
        raise ValueError("Expected exactly the count, mean, and sum rows")
    expected = {
        "count": (4, 4),
        "mean": (160, 16),
        "sum": (640, 64),
    }
    for row in rows:
        values = (float(row["revenue"]), float(row["units"]))
        if not all(math.isfinite(value) for value in values) or values != expected[row["metric"]]:
            raise ValueError(f"Incorrect synthetic-data summary for {row['metric']}")
    return rows


async def analyze(pool, session_id, endpoint, model):
    # MCP credentials are separate; these credentials are only for Foundry inference.
    async with DefaultAzureCredential() as credential:
        async with AIProjectClient(endpoint=endpoint, credential=credential) as project:
            client = FoundryChatClient(
                project_client=project, model=model,
                function_invocation_configuration={
                    "max_iterations": 8, "max_function_calls": 8,
                    "max_duration_seconds": 300, "allow_concurrent_invocation": False,
                },
            )
            async with Agent(
                client=client, name="confidential-spreadsheet-analyst",
                instructions=(
                    "Use the provided tools for all calculations. Code executes in confidential ACI, "
                    "not in this process. Only use the synthetic file. Tool output goes to Foundry; "
                    "keep it to the requested aggregate. Do not claim success without tool evidence."
                ),
                tools=session_tools(pool, session_id),
                default_options={"store": False},
            ) as agent:
                async with asyncio.timeout(300):
                    return await agent.run(PROMPT)


async def main():
    config = PoolConfig.from_env()
    endpoint = required_setting("FOUNDRY_PROJECT_ENDPOINT")
    model = required_setting("FOUNDRY_MODEL")
    output = EXAMPLES / "agent_summary.csv"
    async with PoolClient(config) as pool:
        async with pool.session() as session_id:
            await pool.upload(session_id, EXAMPLES / "sample_sales.xlsx")
            response = await analyze(pool, session_id, endpoint, model)
            await pool.download(session_id, "summary.csv", output)
            validate_summary(output)
            print(response.text)
    print(f"Verified {output.name}; session released (worker deletion is asynchronous).")


if __name__ == "__main__":
    asyncio.run(main())
