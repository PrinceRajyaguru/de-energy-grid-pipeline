# Databricks notebook source
# MAGIC %pip install azure-storage-blob
# MAGIC dbutils.library.restartPython()

# COMMAND ----------

import re
import xml.etree.ElementTree as ET
from datetime import datetime, timedelta
from azure.storage.blob import BlobServiceClient

storage_key = dbutils.secrets.get(scope="adls_secrets", key="storage_key")

conn_str = f"DefaultEndpointsProtocol=https;AccountName=stdeenergygriddev;AccountKey={storage_key};EndpointSuffix=core.windows.net"
blob_service = BlobServiceClient.from_connection_string(conn_str)
container = blob_service.get_container_client("bronze")

# COMMAND ----------

PSR_TYPES = {
    "B01": "Biomass", "B02": "Fossil Brown coal/Lignite", "B03": "Fossil Coal-derived gas",
    "B04": "Fossil Gas", "B05": "Fossil Hard coal", "B06": "Fossil Oil", "B07": "Fossil Oil shale",
    "B08": "Fossil Peat", "B09": "Geothermal", "B10": "Hydro Pumped Storage",
    "B11": "Hydro Run-of-river and poundage", "B12": "Hydro Water Reservoir",
    "B13": "Marine", "B14": "Nuclear", "B15": "Other renewable", "B16": "Solar",
    "B17": "Waste", "B18": "Wind Offshore", "B19": "Wind Onshore", "B20": "Other"
}

def local(tag):
    return tag.split('}')[-1] if '}' in tag else tag

def parse_iso(ts):
    return datetime.strptime(ts.replace("Z", ""), "%Y-%m-%dT%H:%M")

def resolution_minutes(res_str):
    if res_str == "PT15M": return 15
    if res_str == "PT30M": return 30
    if res_str in ("PT60M", "PT1H"): return 60
    if res_str == "P1Y": return None
    return 60

def parse_xml_bytes(xml_bytes, dataset_type):
    rows, gaps = [], []
    try:
        root = ET.fromstring(xml_bytes)
    except ET.ParseError:
        return rows, gaps

    if local(root.tag) == "Acknowledgement_MarketDocument":
        return rows, gaps

    neighbor_zone, direction = None, None
    m = re.match(r"flow_([A-Z0-9]+)_(import|export)", dataset_type)
    if m:
        neighbor_zone, direction = m.group(1), m.group(2)

    for ts in root:
        if local(ts.tag) != "TimeSeries":
            continue

        psr_type, flow_direction, classification_sequence, business_type = None, None, None, None
        for child in ts:
            ctag = local(child.tag)
            if ctag == "MktPSRType":
                for c2 in child:
                    if local(c2.tag) == "psrType":
                        psr_type = c2.text
            if ctag == "businessType":
                business_type = child.text
            if ctag == "classificationSequence_AttributeInstanceComponent.position":
                classification_sequence = child.text

        if dataset_type == "generation":
            flow_direction = "consumption" if business_type == "A04" else "generation"

        if dataset_type == "price" and classification_sequence not in (None, "1"):
            continue

        for period in ts:
            if local(period.tag) != "Period":
                continue
            res_str, start_dt, end_dt, points = None, None, None, {}
            for pchild in period:
                ptag = local(pchild.tag)
                if ptag == "resolution":
                    res_str = pchild.text
                elif ptag == "timeInterval":
                    for tc in pchild:
                        if local(tc.tag) == "start": start_dt = parse_iso(tc.text)
                        if local(tc.tag) == "end": end_dt = parse_iso(tc.text)
                elif ptag == "Point":
                    pos, qty = None, None
                    for c in pchild:
                        if local(c.tag) == "position": pos = int(c.text)
                        if local(c.tag) == "quantity": qty = float(c.text)
                    if pos is not None:
                        points[pos] = qty

            if start_dt is None:
                continue

            if res_str == "P1Y":
                val = points.get(1)
                rows.append(dict(timestamp=start_dt, resolution_minutes=None, dataset_type=dataset_type,
                    psr_type=PSR_TYPES.get(psr_type, psr_type), flow_direction=flow_direction,
                    neighbor_zone=neighbor_zone, direction=direction, value=val, unit="MW"))
                continue

            res_min = resolution_minutes(res_str)
            total_periods = int((end_dt - start_dt).total_seconds() / 60 / res_min)
            last_val = None
            for pos in range(1, total_periods + 1):
                ts_val = start_dt + timedelta(minutes=res_min * (pos - 1))
                if pos in points:
                    last_val = points[pos]
                    rows.append(dict(timestamp=ts_val, resolution_minutes=res_min, dataset_type=dataset_type,
                        psr_type=PSR_TYPES.get(psr_type, psr_type), flow_direction=flow_direction,
                        neighbor_zone=neighbor_zone, direction=direction, value=last_val, unit="MW"))
                elif last_val is not None:
                    rows.append(dict(timestamp=ts_val, resolution_minutes=res_min, dataset_type=dataset_type,
                        psr_type=PSR_TYPES.get(psr_type, psr_type), flow_direction=flow_direction,
                        neighbor_zone=neighbor_zone, direction=direction, value=last_val, unit="MW"))
                else:
                    gaps.append(dict(dataset_type=dataset_type, missing_timestamp=ts_val))

    return rows, gaps

# COMMAND ----------

all_rows, all_gaps = [], []
blob_names = [b.name for b in container.list_blobs() if b.name.endswith(".xml")]
print(f"Processing {len(blob_names)} files...")

for name in blob_names:
    dataset_type = name.split("/")[0]
    date_match = re.search(r"(\d{4}-\d{2}-\d{2})", name)
    target_date = datetime.strptime(date_match.group(1), "%Y-%m-%d") if date_match else None

    blob_client = container.get_blob_client(name)
    xml_bytes = blob_client.download_blob().readall()
    rows, gaps = parse_xml_bytes(xml_bytes, dataset_type)

    if target_date is not None:
        day_start, day_end = target_date, target_date + timedelta(days=1)
        rows = [r for r in rows if r["resolution_minutes"] is None or (day_start <= r["timestamp"] < day_end)]

    all_rows.extend(rows)
    all_gaps.extend(gaps)
    print(f"  {name}: {len(rows)} rows, {len(gaps)} gaps")

print(f"\nTotal: {len(all_rows)} rows, {len(all_gaps)} gaps")

silver_df = spark.createDataFrame(all_rows)
silver_df.write.mode("overwrite").saveAsTable("entsoe_silver")

if all_gaps:
    gaps_df = spark.createDataFrame(all_gaps)
    gaps_df.write.mode("overwrite").saveAsTable("entsoe_silver_gaps")

print("Done.")