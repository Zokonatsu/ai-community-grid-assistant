# -*- coding: utf-8 -*-
"""
scripts/pipeline.py — 测试 + 部署上线 全流程（测试智能体 tester 的主命令）

用法：
    python scripts/pipeline.py test          # 本地 4 步测试（= quick_test full，约 4 分钟）
    python scripts/pipeline.py docker        # 镜像构建 + Trivy（本机有 docker/trivy 时；无则 SKIP）
    python scripts/pipeline.py deploy        # 部署上线：备份 -> 上传 -> 服务器构建 -> 启动 -> 冒烟
    python scripts/pipeline.py all           # test -> docker -> deploy（完整上线流程）

部署所需环境变量（不写入任何文件/仓库）：
    SSH_HOST   服务器 IP，默认 118.31.58.191
    SSH_USER   服务器用户，默认 root
    SSH_PASS   密码（或 SSH_KEY 私钥路径，二选一）
    PIP_INDEX_URL  服务器构建 pip 镜像源，默认 https://mirrors.aliyun.com/pypi/simple/
    DEPLOY_CONFIRM=yes  跳过 deploy 前的确认提示（CI/无人值守）

流程说明（与 docs/coordination/README.md 联动）：
    test 失败 -> 输出问题报告（tester 智能体自动交给 planner）；
    test 通过 -> 可继续 docker -> deploy -> smoke，全部通过即上线完成。
"""
import os
import subprocess
import sys
import time

PROJECT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PY = sys.executable

SSH_HOST = os.environ.get("SSH_HOST", "118.31.58.191")
SSH_USER = os.environ.get("SSH_USER", "root")
SSH_PASS = os.environ.get("SSH_PASS", "")
SSH_KEY = os.environ.get("SSH_KEY", "")
PIP_INDEX_URL = os.environ.get("PIP_INDEX_URL", "https://mirrors.aliyun.com/pypi/simple/")
REMOTE = "/root/ai-community-grid-assistant"
IMAGE = "ai-community-grid-assistant_app:latest"
CONTAINER = "ai-community-grid-assistant_app_1"


# ---------------------------------------------------------------------------
# 本地测试
# ---------------------------------------------------------------------------
def step_test():
    print("=" * 70)
    print("  阶段 1/3：本地测试（4 步）")
    print("=" * 70)
    cmd = [PY, os.path.join(PROJECT, "scripts", "quick_test.py"), "full"]
    return subprocess.call(cmd, cwd=PROJECT) == 0


# ---------------------------------------------------------------------------
# 镜像构建 + Trivy（本机）
# ---------------------------------------------------------------------------
def step_docker():
    print("=" * 70)
    print("  阶段 2/3：镜像构建 + Trivy 检查（本机）")
    print("=" * 70)
    if not _which("docker"):
        print("[SKIP] 本机无 docker：镜像构建与 Trivy 由 CI 覆盖（.github/workflows/ci.yml image-security），跳过本地构建")
        return True
    ok = True
    r = subprocess.call(["docker", "build", "-f", os.path.join(PROJECT, "deploy", "Dockerfile"),
                         "-t", "grid-assistant:test", PROJECT], cwd=PROJECT)
    ok = ok and r == 0
    print(f"[{'PASS' if r == 0 else 'FAIL'}] docker build（{r}）")
    if _which("trivy"):
        r = subprocess.call(["trivy", "image", "--severity", "HIGH,CRITICAL", "--ignore-unfixed",
                             "--exit-code", "1", "grid-assistant:test"])
        ok = ok and r == 0
        print(f"[{'PASS' if r == 0 else 'FAIL'}] trivy 漏洞扫描（{r}）")
    else:
        print("[SKIP] 本机无 trivy：漏洞扫描由 CI 覆盖，跳过")
    return ok


def _which(name):
    try:
        return subprocess.run(["where", name] if os.name == "nt" else ["which", name],
                              capture_output=True).returncode == 0
    except Exception:
        return False


# ---------------------------------------------------------------------------
# 部署上线（服务器）
# ---------------------------------------------------------------------------
def step_deploy():
    print("=" * 70)
    print("  阶段 3/3：部署上线 -> 冒烟")
    print("=" * 70)
    if os.environ.get("DEPLOY_CONFIRM") != "yes":
        print("[ABORT] 生产部署需显式确认：设置环境变量 DEPLOY_CONFIRM=yes 后再执行。")
        print("        （部署会备份并在服务器重建容器；请确认已通过全部测试）")
        return False
    if not (SSH_PASS or SSH_KEY):
        print("[ABORT] 缺少服务器凭据：设置 SSH_PASS 或 SSH_KEY（SSH_HOST/SSH_USER 可选）")
        return False
    try:
        import paramiko
    except ImportError:
        print("[ABORT] 缺少 paramiko，请先 pip install paramiko")
        return False

    client = paramiko.SSHClient()
    client.set_missing_host_key_policy(paramiko.AutoAddPolicy())
    print(f"  连接 {SSH_USER}@{SSH_HOST} ...", flush=True)
    client.connect(SSH_HOST, 22, SSH_USER, password=SSH_PASS or None, key_filename=SSH_KEY or None, timeout=30)
    ok = True
    try:
        ts = time.strftime("%Y%m%d-%H%M%S")
        old = f"{REMOTE}.old.{ts}"
        backup = f"/root/backup-ai-community-grid-assistant-{ts}.tar.gz"

        def run(cmd, critical=True):
            nonlocal ok
            print(f"\n$ {cmd}", flush=True)
            _, out, err = client.exec_command(cmd, timeout=1800)
            o = out.read().decode("utf-8", errors="replace")
            e = err.read().decode("utf-8", errors="replace")
            rc = out.channel.recv_exit_status()
            if o.strip():
                print(o, flush=True)
            if e.strip():
                print("[stderr]", e, flush=True)
            print(f"[exit={rc}]", flush=True)
            if critical and rc != 0:
                ok = False
            return rc

        # 1) 备份
        run(f"test -d {REMOTE} && tar czf {backup} -C / root/ai-community-grid-assistant && echo BACKUP_OK || echo NO_EXIST")
        # 2) 旧目录处理（含 .env 视为在产，挪走；否则清理）
        run(f"if [ -d {REMOTE} ]; then if [ -f {REMOTE}/.env ]; then mv {REMOTE} {old} && echo MOVED_OLD; else rm -rf {REMOTE} && echo CLEANED; fi; fi")
        run(f"mkdir -p {REMOTE} && echo DIR_OK")
        # 3) 上传 git 跟踪文件 + .git
        files = subprocess.run(["git", "-C", PROJECT, "ls-files"], capture_output=True, text=True, encoding="utf-8").stdout.splitlines()
        print(f"\n[upload] {len(files)} tracked files", flush=True)
        sftp = client.open_sftp()
        n = 0
        for rel in files:
            local = os.path.join(PROJECT, rel.replace("/", os.sep))
            if not os.path.isfile(local):
                continue
            remote = REMOTE + "/" + rel
            parent = os.path.dirname(remote)
            parts = parent.split("/")
            cur = ""
            for part in parts:
                if not part:
                    continue
                cur = cur + "/" + part
                try:
                    sftp.stat(cur)
                except IOError:
                    sftp.mkdir(cur)
            sftp.put(local, remote)
            if rel.endswith(".sh"):
                sftp.chmod(remote, 0o755)
            n += 1
        # 上传 .git（保留完整 git 仓库）
        for rootdir, dirs, fs in os.walk(os.path.join(PROJECT, ".git")):
            dirs[:] = [d for d in dirs if d not in ("lfs", "worktrees")]
            for f in fs:
                full = os.path.join(rootdir, f)
                rel = os.path.relpath(full, PROJECT).replace(os.sep, "/")
                remote = REMOTE + "/" + rel
                parent = os.path.dirname(remote)
                parts = parent.split("/")
                cur = ""
                for part in parts:
                    if not part:
                        continue
                    cur = cur + "/" + part
                    try:
                        sftp.stat(cur)
                    except IOError:
                        sftp.mkdir(cur)
                sftp.put(full, remote)
        sftp.close()
        print("[upload] done", flush=True)
        # 4) 恢复运行数据
        run(f"if [ -d {old} ]; then cp -a {old}/.env {REMOTE}/.env && cp -a {old}/data {REMOTE}/data && cp -a {old}/secure {REMOTE}/secure && echo RESTORE_OK; fi")
        # 5) 服务器构建 + 启动
        run(f"cd {REMOTE} && docker rm -f {CONTAINER} 2>/dev/null; docker build -t {IMAGE} --build-arg PIP_INDEX_URL={PIP_INDEX_URL} -f deploy/Dockerfile .", critical=False, timeout=1800)
        if ok:
            run(f"docker run -d --name {CONTAINER} --restart unless-stopped -p 8000:8000 --env-file {REMOTE}/.env -v {REMOTE}/data:/app/data -v {REMOTE}/secure:/app/secure {IMAGE}")
            run("sleep 6; docker ps --filter name=ai-community-grid-assistant --format '{{.Names}} | {{.Status}} | {{.Ports}}'")
        else:
            print("[ABORT] 镜像构建失败，未启动新容器", flush=True)
    finally:
        client.close()
    return ok


# ---------------------------------------------------------------------------
# 冒烟（部署后）
# ---------------------------------------------------------------------------
def step_smoke():
    print("=" * 70)
    print("  部署冒烟")
    print("=" * 70)
    base = f"http://{SSH_HOST}:8000"
    cmd = [PY, os.path.join(PROJECT, "scripts", "smoke_test.py"), base]
    return subprocess.call(cmd, cwd=PROJECT) == 0


def main():
    mode = (sys.argv[1] if len(sys.argv) > 1 else "test").lower()
    if mode == "test":
        ok = step_test()
    elif mode == "docker":
        ok = step_docker()
    elif mode == "deploy":
        ok = step_deploy() and step_smoke()
    elif mode == "all":
        ok = step_test() and step_docker() and step_deploy() and step_smoke()
    else:
        print(__doc__)
        return 2
    print("=" * 70)
    print("  流水线结果：" + ("通过（OK）" if ok else "未通过"))
    print("=" * 70)
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())