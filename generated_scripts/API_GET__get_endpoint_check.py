# API Test Step: GET /get endpoint check
# Target URL: https://httpbin.org/get

import json
import requests
import time

def run_api_test():
    url = "https://httpbin.org/get"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZF91c2VyIjoyMiwiaWRfZGV2aWNlIjo0LCJzZXJpYWxfbnVtYmVyIjoiV0VCLTE3OTE1MTkyNjEwNjItNWc3d3Z3eWNmc20iLCJ2ZXJzaW9uX251bWJlciI6IjEuMCIsImF1ZCI6ImxvY2FsaG9zdCIsImlzcyI6ImxvY2FsaG9zdCIsImlhdCI6MTc5MTUxOTI2MywiZXhwIjoxNzk0MTExMjYzfQ.bKuXER4xXv0YcfGBi3RpEclUrHd-ecA9OdunpI8hjkY"}
    t_start = time.time()
    response = requests.request("GET", url, headers=headers, timeout=10)
    latency = round((time.time() - t_start) * 1000, 2)
    print(f'HTTP {response.status_code} in {latency}ms')
    try:
        print('Response:', json.dumps(response.json(), indent=2)[:500])
    except Exception:
        print('Response Text:', response.text[:500])
    assert response.status_code in [200, 201], f'Expected HTTP in [200, 201], got {response.status_code}: {response.text[:200]}'

if __name__ == '__main__':
    run_api_test()
