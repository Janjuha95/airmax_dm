"""airquality — Belgian air-quality ingestion, aggregation, and visualisation.

A small pipeline: SQS (OpenAQ simulator) -> boto3 consumer -> local Postgres -> a SQL
3-hour view -> Folium maps + WHO/IRCEL insights. See the project README for context.
"""

__version__ = "1.0.0"
