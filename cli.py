import os
import typer
from typing import Optional
from rich.console import Console

from core.ai_client import AIClient
from core.test_generator import TestGenerator
from runners.api_runner import APIRunner
from runners.sheet_runner import SheetRunner
from runners.web_runner import WebRunner

app = typer.Typer(help="🚀 AI-Powered QA Testing Tool CLI")
console = Console()


@app.command()
def api(
    spec: str = typer.Argument(..., help="Path or URL to OpenAPI/Swagger JSON"),
    url: str = typer.Argument(..., help="Base URL for the API (e.g. https://api.example.com)"),
    output: str = typer.Option("api_test_results.csv", help="Output file path (.csv or .xlsx)"),
):
    """Run automated API tests from an OpenAPI specification."""
    console.print(f"[bold cyan]🔍 Inspecting OpenAPI Spec:[/] {spec} against [bold]{url}[/]")
    runner = APIRunner(url=url)
    runner.run_tests_from_spec(spec_path_or_url=spec, base_url=url, output_csv=output)


@app.command()
def web(
    url: str = typer.Argument(..., help="Target Web URL"),
    step: Optional[str] = typer.Option("Verify page loads and main components are visible", help="Test step description"),
    expected: Optional[str] = typer.Option("Page displays correctly without error", help="Expected result"),
    headed: bool = typer.Option(False, "--headed", help="Run browser in visible (headed) mode"),
    visual: bool = typer.Option(False, "--visual", help="Run visual AI layout inspection"),
):
    """Run an AI-powered Web UI test step with self-healing on a target URL."""
    console.print(f"[bold cyan]🌐 Starting Web UI Test on:[/] {url}")
    runner = WebRunner(
        url=url,
        headed=headed,
        visual=visual,
        test_step=step,
        expected_result=expected,
    )
    runner.execute()


@app.command()
def sheet(
    file: str = typer.Argument(..., help="Input test suite file (.csv or .xlsx)"),
    output: str = typer.Option("test_results.xlsx", help="Output results file (.csv or .xlsx)"),
    headed: bool = typer.Option(False, "--headed", help="Run browser in headed mode for web steps"),
    visual: bool = typer.Option(False, "--visual", help="Enable AI visual layout checks"),
):
    """Run an entire test suite from a CSV or Excel file."""
    console.print(f"[bold green]📋 Running Test Suite from:[/] {file}")
    runner = SheetRunner(
        file_path=file,
        output_path=output,
        headed=headed,
        visual=visual,
    )
    runner.run_suite()


@app.command()
def generate(
    url: str = typer.Argument(..., help="Target web URL to scan"),
    output: str = typer.Option("generated_tests.csv", help="Output CSV file for generated test cases"),
    execute_now: bool = typer.Option(False, "--execute", help="Automatically execute the generated suite after creation"),
):
    """Dynamically scan a webpage, extract actionable elements, and generate test cases."""
    console.print(f"[bold magenta]✨ Auto-generating test cases for:[/] {url}")
    ai = AIClient()
    generator = TestGenerator(ai)
    output_path = generator.generate_cases_from_url(url=url, output_csv_path=output)

    if output_path and execute_now:
        console.print("\n[bold cyan]▶️ Auto-executing generated suite...[/]")
        runner = SheetRunner(
            file_path=output_path, output_path="generated_tests_results.csv"
        )
        runner.run_suite()


if __name__ == "__main__":
    app()
