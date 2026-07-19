from datetime import datetime


class ReportManager:
    def __init__(self):
        self.results = []

    def add(
        self,
        transmittal,
        status,
        mapped=0,
        remarks=""
    ):
        self.results.append({
            "timestamp": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
            "transmittal": str(transmittal),
            "status": status,
            "mapped": mapped,
            "remarks": remarks
        })

    def success(self, transmittal, mapped):
        self.add(
            transmittal=transmittal,
            status="SUCCESS",
            mapped=mapped
        )

    def skipped(self, transmittal, remarks):
        self.add(
            transmittal=transmittal,
            status="SKIPPED",
            remarks=remarks
        )

    def failed(self, transmittal, remarks):
        self.add(
            transmittal=transmittal,
            status="FAILED",
            remarks=remarks
        )

    def summary(self):
        total = len(self.results)

        success = sum(
            1 for r in self.results
            if r["status"] == "SUCCESS"
        )

        skipped = sum(
            1 for r in self.results
            if r["status"] == "SKIPPED"
        )

        failed = sum(
            1 for r in self.results
            if r["status"] == "FAILED"
        )

        return {
            "total": total,
            "success": success,
            "skipped": skipped,
            "failed": failed
        }


report = ReportManager()