# Databricks notebook source
# MAGIC %pip install azure-storage-blob
# MAGIC %pip install deltalake

# COMMAND ----------

import io
from azure.storage.blob import BlobServiceClient

storage_key = dbutils.secrets.get(scope="adls_secrets", key="storage_key")
conn_str = f"DefaultEndpointsProtocol=https;AccountName=stdeenergygriddev;AccountKey={storage_key};EndpointSuffix=core.windows.net"
blob_service = BlobServiceClient.from_connection_string(conn_str)

# COMMAND ----------

from pyspark.sql import functions as F

silver = spark.table("entsoe_silver")
silver = silver.withColumn("hour", F.date_trunc("hour", F.col("timestamp")))

# COMMAND ----------

gen_15 = silver.filter((F.col("dataset_type") == "generation") & (F.col("flow_direction") == "generation"))

# Average the 15-min points within each hour, per source, first
gen_per_source_hour = gen_15.groupBy("hour", "psr_type").agg(F.avg("value").alias("avg_mw"))

gen = (gen_per_source_hour.groupBy("hour")
       .agg(F.sum("avg_mw").alias("total_generation_mw"),
            F.sum(F.when(F.col("psr_type").isin(
                "Wind Onshore","Wind Offshore","Solar","Biomass",
                "Hydro Run-of-river and poundage","Hydro Water Reservoir","Marine","Other renewable"
            ), F.col("avg_mw")).otherwise(0)).alias("renewable_generation_mw")))

# COMMAND ----------

load = (silver.filter(F.col("dataset_type") == "load")
        .groupBy("hour").agg(F.avg("value").alias("total_load_mw")))

price = (silver.filter(F.col("dataset_type") == "price")
         .groupBy("hour").agg(F.avg("value").alias("day_ahead_price_eur_mwh")))

# COMMAND ----------

flows = silver.filter(F.col("dataset_type").startswith("flow_"))

# Average the 15-min points within each hour, per zone/direction, first
flows_avg = flows.groupBy("hour", "neighbor_zone", "direction").agg(F.avg("value").alias("avg_mw"))

imports = flows_avg.filter(F.col("direction") == "import").groupBy("hour").agg(F.sum("avg_mw").alias("import_mw"))
exports = flows_avg.filter(F.col("direction") == "export").groupBy("hour").agg(F.sum("avg_mw").alias("export_mw"))

# COMMAND ----------

gold = (gen.join(load, "hour", "outer")
        .join(price, "hour", "outer")
        .join(imports, "hour", "outer")
        .join(exports, "hour", "outer")
        .fillna(0, subset=["import_mw", "export_mw"])
        .withColumn("net_import_mw", F.col("import_mw") - F.col("export_mw"))
        .withColumn("renewable_share_pct", F.round(F.col("renewable_generation_mw") / F.col("total_generation_mw") * 100, 1))
        .withColumn("stress_flag",
            F.when((F.col("renewable_share_pct") >= 65) & (F.col("day_ahead_price_eur_mwh") <= 20), "oversupply_risk")
            .when(F.col("net_import_mw") > F.col("total_load_mw") * 0.15, "shortfall_risk")
            .otherwise("normal"))
        .select("hour", "total_generation_mw", "renewable_generation_mw", "renewable_share_pct",
                "total_load_mw", "net_import_mw", "day_ahead_price_eur_mwh", "stress_flag")
        .orderBy("hour"))

gold.write.mode("overwrite").saveAsTable("entsoe_gold_hourly")
display(gold)

# COMMAND ----------

import pandas as pd
from deltalake import write_deltalake

storage_options = {
    "account_name": "stdeenergygriddev",
    "account_key": storage_key,
}

def write_delta(df, container_name, table_name):
    rows = [row.asDict() for row in df.collect()]
    pdf = pd.DataFrame(rows, columns=df.columns)
    # An all-null column becomes a Null-typed Arrow column, which Delta rejects; cast from the Spark schema
    for field in df.schema.fields:
        t = field.dataType.simpleString()
        if t in ("double", "float") or t.startswith("decimal"):
            pdf[field.name] = pdf[field.name].astype("float64")
        elif t in ("bigint", "int", "smallint", "tinyint"):
            pdf[field.name] = pdf[field.name].astype("Int64")
        elif t == "string":
            pdf[field.name] = pdf[field.name].astype("string")
        elif t.startswith("timestamp"):
            pdf[field.name] = pd.to_datetime(pdf[field.name])
    path = f"abfss://{container_name}@stdeenergygriddev.dfs.core.windows.net/{table_name}"
    write_deltalake(path, pdf, storage_options=storage_options, mode="overwrite")
    print(f"Wrote Delta table: {path}")

write_delta(gold, "gold", "entsoe_gold_hourly")

# COMMAND ----------

gold.groupBy("stress_flag").count().show()