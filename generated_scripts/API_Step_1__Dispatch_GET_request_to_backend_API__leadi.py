# API Test Step: Step 1: Dispatch GET request to backend API 'leading-subsidies' (http://65.1.14.3:3337/api/v1/file/dashboard/hdc/leading-subsidies) -> Step 2: Verify HTTP 200/201 response status and JSON payload.
# Target URL: http://65.1.14.3:3337/api/v1/file/dashboard/hdc/leading-subsidies

import json
import requests
import time

def run_api_test():
    url = "http://65.1.14.3:3337/api/v1/file/dashboard/hdc/leading-subsidies"
    headers = {"Content-Type": "application/json", "Authorization": "Bearer eyJhbGciOiJIUzI1NiIsInR5cCI6IkpXVCJ9.eyJlbXBsb3llZUlkIjo5NSwiZGV2aWNlSWQiOjc0LCJzZXJpYWxOdW1iZXIiOiJVbmtub3duIiwidmVyc2lvbk51bWJlciI6IjEuMCIsImF1ZCI6ImxvY2FsaG9zdCIsImlzcyI6ImxvY2FsaG9zdCIsImlhdCI6MTc5MTg3NDE2MiwiZXhwIjoxNzk0NDY2MTYyfQ.Ec4bItVF_hJdWiBqnP6LMxNFNzcC_09yER-SmIoB3Rc"}
    t_start = time.time()
    response = requests.request("GET", url, headers=headers, timeout=10)
    latency = round((time.time() - t_start) * 1000, 2)
    print(f'HTTP {response.status_code} in {latency}ms')
    try:
        print('Response:', json.dumps(response.json(), indent=2)[:500])
    except Exception:
        print('Response Text:', response.text[:500])
    assert response.status_code in [200, 201, 204], f'Expected HTTP in [200, 201, 204], got {response.status_code}: {response.text[:200]}'

if __name__ == '__main__':
    run_api_test()
