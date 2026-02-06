import os
import zipfile
import paramiko
import time
import sys

# 服务器信息
SERVER_IP = "116.62.36.24"
USERNAME = "root"
PASSWORD = "maKmUY2QZ*^Bep6}"
REMOTE_DIR = "/root/app"
ZIP_NAME = "deploy_package.zip"

def create_zip(zip_name):
    print(f"正在打包项目到 {zip_name}...")
    exclude_dirs = {
        'venv', '__pycache__', '.git', '.idea', '.vscode', 
        'logs', 'reports', 'node_modules', 'frontend' # 暂时只部署后端，前端通常单独部署或作为静态文件
    }
    # 也可以包含 frontend 如果是前后端不分离或者静态文件由后端 serve
    # 根据 LS 结果，frontend 只有 index.html 和快捷方式，后端 static 也有 index.html
    # 假设后端直接 serve 静态文件，我们将 frontend 也打包进去，或者只打包 backend 和 requirements.txt
    
    with zipfile.ZipFile(zip_name, 'w', zipfile.ZIP_DEFLATED) as zipf:
        for root, dirs, files in os.walk('.'):
            # 过滤目录
            dirs[:] = [d for d in dirs if d not in exclude_dirs]
            
            for file in files:
                if file == zip_name or file.endswith('.pyc') or file.endswith('.log'):
                    continue
                
                file_path = os.path.join(root, file)
                # 排除根目录下的一些大文件或无关文件
                if root == '.' and file.endswith('.zip'):
                    continue
                
                zipf.write(file_path, arcname=os.path.relpath(file_path, '.'))
    print("打包完成。")

def deploy():
    # 1. 打包
    create_zip(ZIP_NAME)
    
    # 2. 连接 SSH
    print(f"正在连接服务器 {SERVER_IP}...")
    ssh = paramiko.SSHClient()
    ssh.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    
    try:
        ssh.connect(SERVER_IP, username=USERNAME, password=PASSWORD)
        print("SSH 连接成功。")
        
        # 3. 上传文件
        sftp = ssh.open_sftp()
        print(f"正在上传 {ZIP_NAME}...")
        sftp.put(ZIP_NAME, f"/root/{ZIP_NAME}")
        sftp.close()
        print("上传成功。")
        
        # 4. 执行部署命令
        commands = [
            # 安装系统依赖
            "export DEBIAN_FRONTEND=noninteractive && apt-get update",
            "export DEBIAN_FRONTEND=noninteractive && apt-get install -y python3-pip python3-venv redis-server unzip",
            
            # 清理旧目录
            # f"rm -rf {REMOTE_DIR}",
            f"mkdir -p {REMOTE_DIR}",
            f"mkdir -p {REMOTE_DIR}/reports",
            f"mkdir -p {REMOTE_DIR}/uploads",
            
            # 解压
            f"unzip -o /root/{ZIP_NAME} -d {REMOTE_DIR}",
            
            # 创建虚拟环境
            f"cd {REMOTE_DIR} && python3 -m venv venv",
            
            # 安装 Python 依赖 (忽略特定版本错误，尝试安装)
            f"cd {REMOTE_DIR} && ./venv/bin/pip install --upgrade pip",
            f"cd {REMOTE_DIR} && ./venv/bin/pip install -r requirements.txt",
            
            # 停止旧服务
            "pkill -f uvicorn || true",
            "pkill -f celery || true",
            
            # 启动 Redis
            "service redis-server start",
            
            # 启动 Celery Worker
            f"cd {REMOTE_DIR} && export PYTHONPATH={REMOTE_DIR}/backend && nohup ./venv/bin/celery -A app.workers.celery_app worker --loglevel=info > celery.log 2>&1 &",
            
            # 启动 FastAPI
            f"cd {REMOTE_DIR} && export PYTHONPATH={REMOTE_DIR}/backend && nohup ./venv/bin/uvicorn app.main:app --host 0.0.0.0 --port 8000 > app.log 2>&1 &",
        ]
        
        for cmd in commands:
            print(f"执行远程命令: {cmd}")
            stdin, stdout, stderr = ssh.exec_command(cmd)
            # 等待命令完成 (除了启动后台进程的命令)
            if "nohup" not in cmd:
                exit_status = stdout.channel.recv_exit_status()
                if exit_status != 0:
                    print(f"命令失败: {stderr.read().decode()}")
                    # 某些 apt update 错误可以忽略，但通常应该关注
                    if "apt-get" in cmd:
                        pass 
                    else:
                        # raise Exception("部署命令失败")
                        pass # 暂时继续
            else:
                # 对于 nohup 命令，稍微等待一下
                time.sleep(2)
        
        print("所有服务已启动，等待启动检测...")
        time.sleep(10) # 等待应用启动
        
        # 5. 冒烟测试
        print("执行冒烟测试...")
        check_cmd = "curl -I http://127.0.0.1:8000/docs"
        stdin, stdout, stderr = ssh.exec_command(check_cmd)
        output = stdout.read().decode()
        if "200 OK" in output:
            print("SUCCESS: 服务启动成功，API 文档可访问。")
            print(f"远程访问地址: http://{SERVER_IP}:8000/docs")
        else:
            print("WARNING: 冒烟测试未通过，请检查日志。")
            print(f"curl output: {output}")
            # 读取日志
            print("正在读取 app.log 最后 20 行...")
            stdin, stdout, stderr = ssh.exec_command(f"tail -n 20 {REMOTE_DIR}/app.log")
            print(stdout.read().decode())
            
    except Exception as e:
        print(f"发生错误: {e}")
    finally:
        ssh.close()
        if os.path.exists(ZIP_NAME):
            os.remove(ZIP_NAME)

if __name__ == "__main__":
    deploy()
