import io
import sys
import time

if sys.stdout and hasattr(sys.stdout, "reconfigure"):
    try:
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass
if sys.stderr and hasattr(sys.stderr, "reconfigure"):
    try:
        sys.stderr.reconfigure(encoding="utf-8", errors="replace")
    except Exception:
        pass

import pandas as pd
from rich.console import Console
from rich.table import Table

from runners.api_runner import APIRunner
from runners.web_runner import WebRunner


class SheetRunner:
    def __init__(
        self,
        file_path: str,
        output_path: str = "test_results.xlsx",
        headed: bool = False,
        visual: bool = False,
        test_data: dict = None,
    ):
        self.file_path = file_path
        self.output_path = output_path
        self.headed = headed
        self.visual = visual
        self.test_data = test_data or {}
        self.console = Console()

    def run_suite(self):
        self.console.print(f"[bold cyan]🚀 Loading test suite from:[/] {self.file_path}")
        if self.file_path.endswith(".csv"):
            df = pd.read_csv(self.file_path)
        else:
            df = pd.read_excel(self.file_path)

        # Cleanup unnamed columns
        df = df.loc[:, ~df.columns.str.contains(r"^Unnamed")]
        df = df.dropna(how="all")

        for str_col, default_val in [
            ("Status", "PENDING"),
            ("Execution_Notes", ""),
            ("Playwright_Code", ""),
            ("Screenshot", ""),
            ("Actual_Result", "-"),
            ("Duration", "-"),
        ]:
            if str_col not in df.columns:
                df[str_col] = default_val
            df[str_col] = df[str_col].fillna(default_val).astype(str).replace("nan", default_val).astype(object)

        total_tests = len(df)
        passed_count = 0
        failed_count = 0
        skipped_count = 0
        start_time = time.time()

        for loop_idx, (index, row) in enumerate(zip(df.index, df.to_dict("records"))):
            test_type = str(row.get("Type", "")).strip().lower()
            target_url = str(row.get("Target", "")).strip()
            test_desc = str(row.get("Test_Step_Description", "Executing step..."))
            expected_res = str(row.get("Expected_Result", ""))
            custom_script = str(row.get("Playwright_Code", "")).strip()
            if custom_script == "nan":
                custom_script = ""

            self.console.print(
                f"\n[bold yellow][{loop_idx + 1}/{total_tests}][/] [cyan]{test_type.upper()}[/] -> [bold]{test_desc}[/]"
            )

            if target_url and target_url.lower() != "none":
                captured_output = io.StringIO()
                original_stdout = sys.stdout
                sys.stdout = captured_output

                try:
                    if test_type == "api":
                        runner = APIRunner(
                            url=target_url,
                            test_data=self.test_data,
                            test_step=test_desc,
                            expected_result=expected_res,
                            custom_script=custom_script,
                        )
                        gen_code = runner.execute()
                        if gen_code:
                            df.at[index, "Playwright_Code"] = gen_code
                    else:
                        runner = WebRunner(
                            url=target_url,
                            headed=self.headed,
                            visual=self.visual,
                            test_data=self.test_data,
                            test_step=test_desc,
                            expected_result=expected_res,
                            custom_script=custom_script,
                        )
                        gen_code = runner.execute()
                        if gen_code:
                            df.at[index, "Playwright_Code"] = gen_code

                    df.at[index, "Status"] = "PASSED"
                    passed_count += 1
                except Exception as e:
                    print(f"Execution Error: {e}")
                    df.at[index, "Status"] = "FAILED"
                    failed_count += 1
                finally:
                    sys.stdout = original_stdout

                execution_notes = captured_output.getvalue()
                df.at[index, "Execution_Notes"] = execution_notes

                status = df.at[index, "Status"]
                status_color = "green" if status == "PASSED" else "red"
                self.console.print(f"Result: [{status_color} bold]{status}[/]")
            else:
                df.at[index, "Status"] = "SKIPPED"
                df.at[index, "Execution_Notes"] = "Target URL was empty or invalid."
                skipped_count += 1
                self.console.print("Result: [yellow bold]SKIPPED[/]")

        # Save output file
        if self.output_path.endswith(".csv"):
            df.to_csv(self.output_path, index=False)
        else:
            df.to_excel(self.output_path, index=False)

        total_time = round(time.time() - start_time, 2)

        # Summary Table
        table = Table(title="📊 Test Execution Summary", show_header=True, header_style="bold magenta")
        table.add_column("Total Tests", justify="center")
        table.add_column("Passed", justify="center", style="green")
        table.add_column("Failed", justify="center", style="red")
        table.add_column("Skipped", justify="center", style="yellow")
        table.add_column("Success Rate", justify="center", style="bold cyan")
        table.add_column("Duration", justify="center")

        success_rate = f"{(passed_count / total_tests * 100):.1f}%" if total_tests > 0 else "0.0%"
        table.add_row(
            str(total_tests),
            str(passed_count),
            str(failed_count),
            str(skipped_count),
            success_rate,
            f"{total_time}s",
        )
        self.console.print("\n")
        self.console.print(table)
        self.console.print(f"\n[bold green]✅ Results saved to:[/] {self.output_path}\n")

        return df
