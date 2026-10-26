class TaskError(RuntimeError):
    def __init__(self,message,code='TASK_FAILED',retryable=False):
        super().__init__(message);self.code=code;self.retryable=retryable
class Cancelled(TaskError):
    def __init__(self):super().__init__('任务已取消；正式结果保留','CANCELLED')
