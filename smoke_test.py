import httpx
import time
import os
import sys

# 服务器地址
BASE_URL = "http://116.62.36.24:8000"
# 测试文件路径 (使用项目中的 sample_test_results.xlsx)
TEST_FILE = "sample_test_results.xlsx"

def smoke_test():
    print(f"开始对 {BASE_URL} 进行冒烟测试...")
    
    # 设置较长的超时时间，防止网络波动
    client = httpx.Client(timeout=30.0)

    # 1. 检查 API 文档 (服务连通性)
    try:
        resp = client.get(f"{BASE_URL}/docs")
        if resp.status_code == 200:
            print("✅ 步骤 1: API 文档访问正常 (200 OK)")
        else:
            print(f"❌ 步骤 1: API 文档访问失败 ({resp.status_code})")
            return
    except Exception as e:
        print(f"❌ 步骤 1: 无法连接服务器 ({e})")
        return

    # 2. 检查根路径
    try:
        resp = client.get(f"{BASE_URL}/")
        if resp.status_code == 200:
            print(f"✅ 步骤 2: 根路径访问正常: {resp.json()}")
        else:
            print(f"❌ 步骤 2: 根路径访问失败 ({resp.status_code})")
    except Exception as e:
        print(f"❌ 步骤 2: 根路径请求异常 ({e})")

    # 3. 上传文件 (核心业务)
    if not os.path.exists(TEST_FILE):
        print(f"⚠️ 找不到测试文件 {TEST_FILE}，跳过上传测试。")
        return

    job_id = None
    try:
        print(f"正在上传文件 {TEST_FILE}...")
        # httpx 上传文件格式: files={'file': open(...)}
        with open(TEST_FILE, 'rb') as f:
            files = {'file': f}
            resp = client.post(f"{BASE_URL}/api/v1/jobs/upload", files=files)
        
        if resp.status_code == 200:
            data = resp.json()
            job_id = data.get("job_id")
            print(f"✅ 步骤 3: 文件上传成功, Job ID: {job_id}")
        else:
            print(f"❌ 步骤 3: 文件上传失败 ({resp.status_code}): {resp.text}")
            return
    except Exception as e:
        print(f"❌ 步骤 3: 上传过程异常 ({e})")
        return

    # 4. 轮询任务状态
    if not job_id:
        return

    print("正在轮询任务状态 (超时时间 60秒)...")
    start_time = time.time()
    while time.time() - start_time < 60:
        try:
            resp = client.get(f"{BASE_URL}/api/v1/jobs/status/{job_id}")
            if resp.status_code == 200:
                status_data = resp.json()
                status = status_data.get("status")
                logs = status_data.get("logs", [])
                
                print(f"   当前状态: {status} (日志行数: {len(logs)})")
                
                if status == "completed":
                    report_url = status_data.get("report_url")
                    print(f"✅ 步骤 4: 任务执行完成! 报告地址: {report_url}")
                    
                    # 5. 验证报告链接
                    if report_url:
                        full_report_url = f"{BASE_URL}{report_url}"
                        report_resp = client.get(full_report_url)
                        if report_resp.status_code == 200:
                            print(f"✅ 步骤 5: 报告页面可访问 (200 OK)")
                        else:
                            print(f"❌ 步骤 5: 报告页面访问失败 ({report_resp.status_code})")
                    break
                
                elif status == "failed":
                    error = status_data.get("error")
                    print(f"❌ 步骤 4: 任务执行失败: {error}")
                    # 打印最后几行日志
                    print("   最后日志:")
                    for log in logs[-3:]:
                        print(f"   - {log}")
                    break
                
                # 如果还在 running 或 pending，等待一下
                time.sleep(2)
            else:
                print(f"⚠️ 获取状态失败 ({resp.status_code})")
                time.sleep(2)
        except Exception as e:
            print(f"⚠️ 轮询过程异常 ({e})")
            time.sleep(2)
    else:
        print("❌ 步骤 4: 任务轮询超时 (可能 LLM 处理较慢或卡住)")

if __name__ == "__main__":
    smoke_test()
