import json
import os
import requests


class ClickUpClient:
    """
    ClickUp REST API integration client for automated QA bug reporting:
    - Creates bug tasks in ClickUp lists with rich markdown failure diagnostics.
    - Uploads step failure screenshots directly as task attachments.
    - Supports single-task creation and bulk batch filing for failed test suites.
    """

    BASE_URL = "https://api.clickup.com/api/v2"

    def __init__(self, api_token: str = None, default_list_id: str = None):
        self.api_token = (
            api_token
            or os.getenv("CLICKUP_API_TOKEN", "")
            or os.getenv("CLICKUP_API_KEY", "")
        ).strip()
        self.default_list_id = (
            default_list_id
            or os.getenv("CLICKUP_LIST_ID", "")
        ).strip()

    def is_configured(self) -> bool:
        return bool(self.api_token and (self.default_list_id or os.getenv("CLICKUP_LIST_ID")))

    def verify_credentials(self, api_token: str = None) -> dict:
        token = (api_token or self.api_token).strip()
        if not token:
            return {"success": False, "error": "API Token is empty."}
        try:
            res = requests.get(
                f"{self.BASE_URL}/user",
                headers={"Authorization": token, "Content-Type": "application/json"},
                timeout=10,
            )
            if res.status_code == 200:
                data = res.json()
                user = data.get("user", {})
                return {
                    "success": True,
                    "username": user.get("username", "User"),
                    "email": user.get("email", ""),
                }
            else:
                return {"success": False, "error": f"HTTP {res.status_code}: {res.text}"}
        except Exception as e:
            return {"success": False, "error": str(e)}

    def create_bug_task(
        self,
        test_row: dict,
        test_index: int = None,
        list_id: str = None,
        api_token: str = None,
        priority: int = 2,  # 1 = Urgent, 2 = High, 3 = Normal, 4 = Low
        custom_title: str = None,
    ) -> dict:
        """
        Creates a structured bug task in ClickUp from a failed test case row
        and attaches the execution screenshot if available.
        """
        token = (api_token or self.api_token).strip()
        target_list = str(list_id or self.default_list_id).strip()

        if not token:
            return {"success": False, "error": "ClickUp API Token is missing."}
        if not target_list:
            return {"success": False, "error": "ClickUp List ID is missing."}

        # Extract test case metadata
        step_desc = str(test_row.get("Test_Step_Description", "Automated Test Failure")).strip()
        target_url = str(test_row.get("Target", "N/A")).strip()
        expected = str(test_row.get("Expected_Result", "N/A")).strip()
        actual = str(test_row.get("Actual_Result", test_row.get("Status", "FAILED"))).strip()
        duration = str(test_row.get("Duration", "-")).strip()
        t_type = str(test_row.get("Type", "Web")).strip()
        notes = str(test_row.get("Execution_Notes", "")).strip()
        code = str(test_row.get("Playwright_Code", "")).strip()
        ss_path = str(test_row.get("Screenshot", "")).strip()

        idx_prefix = f"Test #{test_index}: " if test_index is not None else ""
        task_name = custom_title or f"[BUG] {idx_prefix}{step_desc[:75]}"

        # Build clean, numbered Steps to Reproduce
        repro_steps = []
        repro_steps.append(f"1. **Navigate to Target:** Open browser and navigate to `{target_url}`.")
        
        # Split step_desc if it has multi-step markers (like ->, ;, and, then, step 1, 1.)
        if any(marker in step_desc.lower() for marker in [" -> ", "\n", ";", "step 1", "1."]):
            raw_parts = [p.strip() for p in re.split(r"(?:\r?\n| -> |;)", step_desc) if p.strip()]
            for p_i, part in enumerate(raw_parts, start=2):
                repro_steps.append(f"{p_i}. **Action:** {part}")
            repro_steps.append(f"{len(raw_parts)+2}. **Observe Result:** Check if system responds according to expected specification.")
        else:
            repro_steps.append(f"2. **Execute Action:** {step_desc}")
            repro_steps.append(f"3. **Verify:** Check that '{expected}' is satisfied.")

        repro_steps_md = "\n".join(repro_steps)

        # Markdown Description
        description = (
            f"## 🐞 Automated QA Bug Report\n\n"
            f"### 🪜 Steps to Reproduce\n"
            f"{repro_steps_md}\n\n"
            f"### 🎯 Expected Result\n"
            f"{expected}\n\n"
            f"### 💥 Actual Result\n"
            f"`{actual}`\n\n"
            f"### 🔍 Test Environment & Details\n"
            f"- **Target URL:** [{target_url}]({target_url})\n"
            f"- **Test Type:** `{t_type}`\n"
            f"- **Execution Time:** `{duration}`\n"
            f"- **Status:** `FAILED`\n\n"
            f"### 📋 Root Cause & Failure Logs\n"
            f"```text\n{notes if notes and notes != 'nan' else 'No failure logs captured.'}\n```\n\n"
        )

        if code and code != "nan":
            description += f"### ⚡ Playwright Automation Script\n```python\n{code}\n```\n\n"

        description += "---\n*Auto-generated by Antigravity AI QA Testing Suite.*"

        payload = {
            "name": task_name,
            "description": description,
            "status": "to do",
            "priority": priority,
            "tags": ["qa-automation", "bug", "leadcrm-test"],
            "notify_all": False,
        }

        try:
            url = f"{self.BASE_URL}/list/{target_list}/task"
            headers = {
                "Authorization": token,
                "Content-Type": "application/json",
            }
            res = requests.post(url, headers=headers, json=payload, timeout=15)
            if res.status_code in [200, 201]:
                task_data = res.json()
                task_id = task_data.get("id")
                task_url = task_data.get("url", f"https://app.clickup.com/t/{task_id}")

                # Upload screenshot as attachment if available
                attachment_msg = ""
                if ss_path and os.path.exists(ss_path):
                    att_res = self._upload_attachment(task_id, ss_path, token)
                    if att_res.get("success"):
                        attachment_msg = " (Screenshot attached)"

                return {
                    "success": True,
                    "task_id": task_id,
                    "task_url": task_url,
                    "task_name": task_name,
                    "message": f"Bug filed successfully in ClickUp! Task #{task_id}{attachment_msg}",
                }
            else:
                return {
                    "success": False,
                    "error": f"ClickUp API Error ({res.status_code}): {res.text}",
                }
        except Exception as e:
            return {"success": False, "error": f"Failed to create ClickUp task: {e}"}

    def _upload_attachment(self, task_id: str, file_path: str, token: str) -> dict:
        """Uploads a file as an attachment to a ClickUp task."""
        try:
            url = f"{self.BASE_URL}/task/{task_id}/attachment"
            headers = {"Authorization": token}
            filename = os.path.basename(file_path)
            with open(file_path, "rb") as f:
                files = {"attachment": (filename, f, "image/png")}
                res = requests.post(url, headers=headers, files=files, timeout=20)
                if res.status_code in [200, 201]:
                    return {"success": True}
                return {"success": False, "error": res.text}
        except Exception as e:
            return {"success": False, "error": str(e)}


clickup_client = ClickUpClient()
