"""Run directly: python examples\\pooled_analysis.py (private gateway required)."""

import asyncio
import os

from pool_client import EXAMPLES, PoolClient, PoolConfig, example_path

ANALYSIS = """
import pandas as pd
df = pd.read_excel('input.xlsx')
numeric = df.select_dtypes('number')
summary = numeric.agg(['count', 'mean', 'sum']).round(2)
summary.rename_axis('metric').to_csv('summary.csv')
print(summary.to_string())
"""


async def main():
    config = PoolConfig.from_env()
    source = example_path(os.environ.get("MCP_EXCEL_PATH", "sample_sales.xlsx"))
    output = EXAMPLES / "summary.csv"
    async with PoolClient(config) as pool:
        async with pool.session() as session_id:
            await pool.upload(session_id, source)
            print(await pool.call("run_python", session_id=session_id, code=ANALYSIS, timeout=120))
            await pool.download(session_id, "summary.csv", output)
    print(f"Saved {output.name}; session released (worker deletion is asynchronous).")


if __name__ == "__main__":
    asyncio.run(main())
