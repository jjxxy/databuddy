"""Single-instance compatibility scheduler; Kubernetes uses an independent CronJob."""
import json,time
from .backup_cloud import configured,save

def main():
    if not configured():return
    while True:
        try:print('ATLAS_OFFSITE_BACKUP '+json.dumps(save()),flush=True);delay=86400
        except Exception as exc:print('ATLAS_OFFSITE_BACKUP_DEFERRED '+type(exc).__name__,flush=True);delay=900
        time.sleep(delay)
if __name__=='__main__':main()
