"""Code revision of this process, independent of mutable user state."""
RUNTIME_REVISION='2026.10.30.4'


def public_info():
    return {'revision':RUNTIME_REVISION,'interactive_task_totals':'unlimited',
            'context_overflow':'automatic_recovery','multiple_attachments':True,
            'max_attachments_per_message':20}
