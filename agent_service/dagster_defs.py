from dagster import Definitions, job, op, ScheduleDefinition, DefaultScheduleStatus
from .execution import queue_due

@op
def enqueue_due_pipelines():
    """Schedule immutable Git versions into the single isolated worker queue."""
    queue_due()

@job
def personal_data_schedule():
    enqueue_due_pipelines()

defs=Definitions(jobs=[personal_data_schedule],schedules=[ScheduleDefinition(job=personal_data_schedule,cron_schedule='* * * * *',execution_timezone='Asia/Shanghai',default_status=DefaultScheduleStatus.RUNNING)])
