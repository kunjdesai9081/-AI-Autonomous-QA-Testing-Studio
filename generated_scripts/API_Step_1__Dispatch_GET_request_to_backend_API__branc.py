# API Test Step: Step 1: Dispatch GET request to backend API 'branch' (http://65.1.14.3:3343/api/master/v1/branch?id_business=5&limit=20) -> Step 2: Verify HTTP 200/201 response status and JSON payload.
# Target URL: http://65.1.14.3:3343/api/master/v1/branch?id_business=5&limit=20

import json
import requests
import time

def run_api_test():
    url = "http://65.1.14.3:3343/api/master/v1/branch?id_business=5&limit=20"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJpZF91c2VyIjoyMiwiaWRfZGV2aWNlIjo0LCJzZXJpYWxfbnVtYmVyIjoiV0VCLTE3OTE4MDgyNDkzOTYtazlrdnd4NTdpOGoiLCJ2ZXJzaW9uX251bWJlciI6IjEuMCIsImF1ZCI6ImxvY2FsaG9zdCIsImlzcyI6ImxvY2FsaG9zdCIsImlhdCI6MTc5MTgwODI1MCwiZXhwIjoxNzk0NDAwMjUwfQ.wJsTKulIgmd25t-Bq_nLHfCY9vpv4P5w_tsiGU48LXg"}
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
