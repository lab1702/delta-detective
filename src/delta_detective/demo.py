from pathlib import Path
import shutil
import tempfile
import duckdb
from .config import InvestigationError
from .core import prepare_output, publish
from .loading import literal


def demo(out, overwrite=False):
    out = prepare_output(out, overwrite)
    stage = Path(tempfile.mkdtemp(prefix=".delta-", dir=out.parent))
    try:
        _write_demo(stage)
        publish(stage, out)
    except duckdb.Error:
        raise InvestigationError("Could not generate demo inputs; check output location and available disk space") from None
    finally:
        if stage.exists():
            shutil.rmtree(stage)
    return out / "comparison.yaml"


def _write_demo(out):
    with duckdb.connect() as con:
        con.execute("CREATE TABLE reference AS SELECT * FROM (VALUES (1,40000::DECIMAL(18,2),'East','web'),(2,30000,'West','store'),(3,16000,'East','web'),(4,14000,'North','store')) t(order_id,net_amount,region,source)")
        con.execute("CREATE TABLE current AS SELECT * FROM (VALUES (1,34000::DECIMAL(18,2),'East','web'),(2,30000,'South','store'),(3,16000,'East','web'),(5,2000,'South','web')) t(order_id,net_amount,region,source)")
        for side in ("reference", "current"):
            con.execute(f"COPY {side} TO {literal(out / (side + '.parquet'))} (FORMAT PARQUET)")
    (out / "comparison.yaml").write_text("""mode: snapshots
reference: reference.parquet
current: current.parquet
key: [order_id]
metric:
  name: net_sales
  aggregate: sum
  column: net_amount
  null_policy: error
dimensions: [region, source]
report:
  include_raw_rows: false
""", encoding="utf-8")
