#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BDMV/非BDMV种子处理脚本 - 差异化队列策略最终版
=============================================
【版本变更日志】
2026-03-14 - 初始版本（差异化队列策略）
  - 核心改造：BDMV任务入队列串行处理，非BDMV任务实时处理
  - 函数优化：process_single_task增加is_bdmv_task参数，非BDMV的ISO检测移至该函数
2026-03-14 - 日志格式优化
  - 移除「脚本启动/退出/结束」的分隔线日志
  - 在BDMV打包成功日志前后添加分隔线，方便识别完成打包任务
  - 移除所有颜色相关代码，恢复纯文本日志输出
2026-05-19 - 功能新增适配自动发种脚本
  - 适配独立命名下载目录结构
  - 非BDMV任务：torcp刮削 + rclone转存全部执行成功后
  - 改用 rclone move 稳妥将完整原种子目录整体移动至 /home/boxbox/welldone
  - 新增移动完成明细日志，显示原路径与目标存放路径
  - 仅成功走完整套流程才执行移动，失败不移动原文件
2026-05-19 - 优化修复一：消除find空目录清理报错
  - 空目录清理移除check严格校验，无空目录不抛出错误日志
  - 保留rclone主传输严格校验，真实传输失败依旧告警
2026-05-19 - 优化修复二：强制执行顺序+无条件移动规则定稿
  - 固定执行顺序：torcp执行完毕 → rclone执行完毕 → 最后再移动原目录至welldone
  - 新增规则：无论torcp是否刮削成功、无论rclone是否完成转移，都不阻拦原目录移入welldone
  - 所有子流程全部设置check=False，流程不中断、不终止、不卡任务
2026-05-19 - 优化修复三：ISO文件独立拦截规则
  - 目录存在ISO文件直接全程拦截
  - 跳过torcp刮削、跳过媒体转移、同时跳过原目录归档移入welldone
  - 仅打印日志提示，不执行任何操作
2026-05-20 - 最终修复
  - 修复移动路径：自动移动外层 HHclub/HDSky/全站点外层总目录
  - 修复日志路径：全部日志统一放在 logs_bdmv，不污染 welldone
  - 移除 rclone 非通用参数 --disable DirMove，全版本兼容
2026-05-20 - 增强优化
  - 步骤间延时 2 秒
  - 简化命令日志输出，仅保留命令本身
  - 智能识别两种下载结构，自动正确移动目录
2026-06-10 - 功能修改
  - 移除第三步 rclone 目录归档，不再移动原下载目录
=============================================
"""

import sys
import os
import json
import requests
import subprocess
import shutil
import time
import signal
import fcntl
from datetime import datetime
from threading import Timer

# ========== 核心配置 ==========
CONFIG_FILE = "/home/boxbox/box_qb_config.json"
LOG_FILE = "/home/boxbox/logs_bdmv/main.log"
LOG_TORCP_PATH = "/home/boxbox/logs_bdmv/torcp_process.log"
LOG_RCLONE_PATH = "/home/boxbox/logs_bdmv/rclone_move.log"
# 归档日志已弃用，保留配置不影响运行
LOG_FINISH_MOVE = "/home/boxbox/logs_bdmv/rclone_finish_move.log"
DEFAULT_DOWNLOAD_ROOT = "/home/boxbox/qbittorrent/download"

# 任务队列 - 仅用于BDMV任务
QUEUE_FILE = "/home/boxbox/bdmv_hash_queue.txt"
LOCK_FILE = "/home/boxbox/bdmv_task.lock"

# ========== 工具函数 ==========
def format_seconds(seconds):
    minutes = int(seconds // 60)
    secs = int(seconds % 60)
    if minutes > 0:
        return f"{minutes}分{secs}秒"
    else:
        return f"{secs}秒"

def log(msg, level="INFO"):
    dt = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    log_msg = f"[{dt}] [{level}] {msg}"
    print(log_msg)
    try:
        os.makedirs(os.path.dirname(LOG_FILE), exist_ok=True)
        with open(LOG_FILE, "a", encoding="utf-8") as f:
            f.write(log_msg + "\n")
    except Exception as e:
        print(f"日志写入失败: {e}")

# ========== 文件锁 ==========
def acquire_lock():
    try:
        lock_fd = open(LOCK_FILE, 'w')
        fcntl.flock(lock_fd, fcntl.LOCK_EX | fcntl.LOCK_NB)
        lock_fd.write(f"{os.getpid()}\n")
        lock_fd.flush()
        return lock_fd
    except (IOError, BlockingIOError):
        return None

def release_lock(lock_fd):
    if lock_fd:
        try:
            fcntl.flock(lock_fd, fcntl.LOCK_UN)
            lock_fd.close()
            if os.path.exists(LOCK_FILE):
                os.remove(LOCK_FILE)
        except Exception as e:
            log(f"释放锁失败: {e}", "WARN")

# ========== 任务队列 ==========
def add_to_queue(info_hash):
    try:
        with open(QUEUE_FILE, "a+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            f.seek(0, os.SEEK_END)
            if f.tell() > 0:
                f.seek(f.tell() - 1)
                last_char = f.read(1)
                if last_char != "\n":
                    f.write("\n")
            f.write(info_hash + "\n")
            f.seek(0)
            queue = [line.strip() for line in f if line.strip()]
            if len(queue) > 1 and info_hash in queue[:-1]:
                f.seek(0)
                lines = f.readlines()
                lines = [line for line in lines if line.strip() != info_hash]
                f.seek(0)
                f.truncate()
                f.write("".join(lines))
                if lines and not lines[-1].endswith("\n"):
                    f.write("\n")
                log(f"BDMV任务已存在，未重复添加: {info_hash}")
            else:
                log(f"BDMV任务已加入队列: {info_hash}")
    except Exception as e:
        log(f"加入队列失败: {e}", "ERROR")

def get_next_task():
    try:
        if not os.path.exists(QUEUE_FILE):
            return None
        with open(QUEUE_FILE, "r+", encoding="utf-8") as f:
            fcntl.flock(f, fcntl.LOCK_EX)
            queue = [line.strip() for line in f if line.strip()]
            if not queue:
                f.close()
                if os.path.exists(QUEUE_FILE):
                    os.remove(QUEUE_FILE)
                return None
            task = queue.pop(0)
            f.seek(0)
            f.truncate()
            f.write("\n".join(queue))
            return task
    except Exception as e:
        log(f"获取队列任务失败: {e}", "ERROR")
        return None

# ========== 信号处理 ==========
def signal_handler(signum, frame):
    log(f"捕获终止信号，优雅退出", "WARN")
    sys.exit(1)

# ========== QB交互 ==========
def load_config():
    try:
        with open(CONFIG_FILE, "r", encoding="utf-8") as f:
            cfg = json.load(f)
        return (
            cfg["address"].strip(),
            cfg["username"].strip(),
            cfg["password"].strip(),
            cfg["tmdb_api_key"].strip()
        )
    except Exception as e:
        log(f"加载配置失败: {e}", "ERROR")
        sys.exit(1)

def get_input_with_timeout(prompt, timeout=60):
    print(prompt)
    timer = Timer(timeout, sys.exit)
    timer.start()
    try:
        res = input().strip()
        timer.cancel()
        return res
    except:
        timer.cancel()
        log("输入超时/失败", "ERROR")
        sys.exit(1)

def login_qb(base_url, user, pwd):
    try:
        resp = requests.post(
            f"{base_url}/api/v2/auth/login",
            data={"username": user, "password": pwd},
            timeout=15
        )
        resp.raise_for_status()
        return resp.cookies.get("SID")
    except Exception as e:
        log(f"QB登录失败: {e}", "ERROR")
        sys.exit(1)

def get_torrent_info(base_url, sid, info_hash):
    try:
        resp = requests.get(
            f"{base_url}/api/v2/torrents/info",
            params={"hashes": info_hash},
            headers={"Cookie": f"SID={sid}"},
            timeout=15
        )
        resp.raise_for_status()
        info = resp.json()[0] if resp.json() else None
        if not info:
            log(f"未找到种子信息: {info_hash}", "ERROR")
            return None, None, None
        return info["name"], info["save_path"], info.get("tags", "")
    except Exception as e:
        log(f"获取种子信息失败: {e}", "ERROR")
        return None, None, None

# ========== BDMV检测 ==========
def is_bdmv_valid(bdmv_path):
    if not os.path.exists(bdmv_path) or not os.path.isdir(bdmv_path):
        return False
    stream_dir = os.path.join(bdmv_path, "STREAM")
    if not os.path.isdir(stream_dir):
        return False
    for f in os.listdir(stream_dir):
        f_path = os.path.join(stream_dir, f)
        if f.endswith(".m2ts") and os.path.isfile(f_path) and os.path.getsize(f_path) > 1 * 1024 * 1024:
            return True
    return False

def find_all_bdmv_dirs(root_path):
    bdmv_parent_dirs = set()
    dir_stack = [root_path]
    while dir_stack:
        current_dir = dir_stack.pop()
        try:
            subdirs = [d for d in os.listdir(current_dir) if os.path.isdir(os.path.join(current_dir, d))]
        except PermissionError:
            log(f"无权限访问目录：{current_dir}", "WARN")
            continue
        for subdir in subdirs:
            subdir_path = os.path.join(current_dir, subdir)
            if subdir == "BDMV":
                parent_dir = os.path.dirname(subdir_path)
                if is_bdmv_valid(subdir_path) and parent_dir not in bdmv_parent_dirs:
                    bdmv_parent_dirs.add(parent_dir)
                    log(f"发现有效BDMV目录：{parent_dir}")
                continue
            dir_stack.append(subdir_path)
    return list(bdmv_parent_dirs)

def has_bdmv_folder(save_path):
    return len(find_all_bdmv_dirs(save_path)) > 0

def has_iso_file(save_path):
    for root, dirs, files in os.walk(save_path):
        for file in files:
            if file.lower().endswith(".iso"):
                return True
    return False

# ========== BDMV打包 ==========
def pack_bdmv(save_path):
    bdmv_dirs = find_all_bdmv_dirs(save_path)
    if not bdmv_dirs:
        log("未找到有效BDMV目录", "INFO")
        return
    log(f"找到{len(bdmv_dirs)}个BDMV目录，开始打包", "INFO")
    success = 0
    fail = 0
    for i, bdmv_path in enumerate(bdmv_dirs, 1):
        mv_name = os.path.basename(bdmv_path)
        log_folder = f"/home/boxbox/logs_bdmv/bdmv_packaging/{mv_name}"
        os.makedirs(log_folder, exist_ok=True)
        log_file_path = os.path.join(log_folder, f"{mv_name}.log")
        iso_path = os.path.join(os.path.dirname(bdmv_path), f"{mv_name}.iso")
        if os.path.exists(iso_path):
            log(f"[{i}/{len(bdmv_dirs)}] 跳过{mv_name}：ISO已存在", "INFO")
            success += 1
            continue
        log(f"[{i}/{len(bdmv_dirs)}] 开始打包{mv_name}", "INFO")
        start_time = time.time()
        try:
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"打包开始时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"BDMV路径: {bdmv_path}\n")
                f.write(f"ISO输出路径: {iso_path}\n")
                f.write(f"{'='*60}\n")
            proc = subprocess.run(
                ["genisoimage", "-o", iso_path, "-iso-level", "4", "-allow-lowercase",
                 "-l", "-udf", "-allow-limited-size", bdmv_path],
                check=True, stdout=subprocess.PIPE, stderr=subprocess.PIPE,
                universal_newlines=True, timeout=2*3600
            )
            elapsed_time = time.time() - start_time
            elapsed_str = format_seconds(elapsed_time)
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"打包结束时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"总耗时: {elapsed_str}\n")
                f.write(f"标准输出: {proc.stdout}\n")
                f.write(f"错误输出: {proc.stderr}\n")
                f.write(f"{'='*60}\n")
            if not os.path.exists(iso_path):
                raise FileNotFoundError(f"ISO文件未生成：{iso_path}")
            iso_size = os.path.getsize(iso_path) / (1024**3)
            if iso_size < 1:
                raise ValueError(f"ISO文件过小（{iso_size:.2f}GB），不符合要求")
            shutil.rmtree(bdmv_path)
            log("="*50 + " 打包完成 " + "="*50, "INFO")
            log(f"[{i}/{len(bdmv_dirs)}] {mv_name}打包成功（{iso_size:.2f}GB，耗时{elapsed_str}），已删除原目录", "INFO")
            log("="*50 + " 打包完成 " + "="*50, "INFO")
            success += 1
        except Exception as e:
            log(f"[{i}/{len(bdmv_dirs)}] {mv_name}打包失败: {e}", "ERROR")
            with open(log_file_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'='*60}\n")
                f.write(f"打包失败时间: {datetime.now().strftime('%Y-%m-%d %H:%M:%S')}\n")
                f.write(f"失败原因: {str(e)}\n")
                f.write(f"耗时: {format_seconds(time.time() - start_time)}\n")
                f.write(f"{'='*60}\n")
            fail += 1
    log(f"打包完成：成功{success}个，失败{fail}个，总计{len(bdmv_dirs)}", "INFO")

# ========== 非BDMV处理 ==========
def is_remux(name):
    return "remux" in name.lower()

def is_web_dl(name):
    return "web-dl" in name.lower()

def process_non_bdmv_folders(save_path, name, tags, tmdb_api_key):
    log("未找到 BDMV 文件夹，执行非原盘操作（实时处理，无需队列）")

    inner_path = os.path.join(save_path, name)
    parent_dir = os.path.dirname(inner_path)

    # ========== 智能识别：两种目录结构自动判断 ==========
    if parent_dir.rstrip("/") == DEFAULT_DOWNLOAD_ROOT.rstrip("/"):
        move_src = inner_path
        log(f"[自动识别] 直接下载模式: {os.path.basename(move_src)}")
    else:
        move_src = parent_dir
        log(f"[自动识别] 脚本下载模式: {os.path.basename(move_src)}")

    # 第一步 torcp
    command = [
        "python3", "/home/boxbox/torcp/tp.py",
        inner_path, "-d", f"/home/boxbox/Emby/{name}/", "-s"
    ]
    if tags:
        command.extend(["--imdbid", tags])
    command.extend([
        "--tmdb-api-key", tmdb_api_key,
        "--origin-name", "--emby-bracket"
    ])
    
    log(f"【执行命令1-torcp】 {' '.join(command)}")
    try:
        os.makedirs(os.path.dirname(LOG_TORCP_PATH), exist_ok=True)
        with open(LOG_TORCP_PATH, "a", encoding='utf-8') as log_file:
            subprocess.run(command, stdout=log_file, stderr=subprocess.STDOUT, check=False)
    except Exception as e:
        log(f"torcp执行异常，继续流程: {e}", "WARN")

    time.sleep(2)

    # 第二步 rclone 媒体转移
    if is_web_dl(name):
        rclone_destination = "/home/boxbox/MyEmby/WEB-DL/"
    elif is_remux(name):
        rclone_destination = "/home/boxbox/MyEmby/Remux/"
    else:
        rclone_destination = "/home/boxbox/MyEmby/Encode/"
    
    rclone_command = [
        "rclone", "move",
        f"/home/boxbox/Emby/{name}/",
        rclone_destination,
        "-v", "--stats", "2000s",
        "--transfers", "3",
        "--drive-chunk-size", "32M",
        f"--log-file={LOG_RCLONE_PATH}",
        "--delete-empty-src-dirs"
    ]
    
    log(f"【执行命令2-rclone-媒体转移】 {' '.join(rclone_command)}")
    try:
        subprocess.run(rclone_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        cleanup_command = ["find", "/home/boxbox/Emby", "-type", "d", "-empty", "-delete"]
        subprocess.run(cleanup_command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
    except Exception as e:
        log(f"rclone执行异常，继续流程: {e}", "WARN")

    time.sleep(2)

    # ========= 已移除：第三步 rclone 目录归档，不再移动原下载目录 =========
    log("torcp刮削 + 媒体转移执行完毕，保留原下载目录不动")

# ========== 处理单个任务 ==========
def process_single_task(info_hash, is_bdmv_task):
    try:
        log(f"开始处理任务: {info_hash} (BDMV任务: {is_bdmv_task})", "INFO")
        base_url, user, pwd, tmdb_api_key = load_config()

        sid = login_qb(base_url, user, pwd)
        if not sid:
            log(f"QB登录失败，跳过任务: {info_hash}", "ERROR")
            return

        torrent_name, save_path, tags = get_torrent_info(base_url, sid, info_hash)
        if not torrent_name or not save_path:
            log(f"获取种子信息失败，跳过任务: {info_hash}", "ERROR")
            return

        if has_iso_file(save_path):
            log("检测到ISO镜像文件，全程跳过所有操作：跳过刮削、跳过媒体转移、跳过原目录归档", "INFO")
            return

        if is_bdmv_task:
            log("执行BDMV打包逻辑", "INFO")
            pack_bdmv(save_path)
        else:
            log("执行非BDMV实时处理逻辑", "INFO")
            process_non_bdmv_folders(save_path, torrent_name, tags, tmdb_api_key)
        
        log(f"任务处理完成: {info_hash}", "INFO")
    except Exception as e:
        log(f"处理任务{info_hash}异常: {e}", "ERROR")

# ========== 主函数 ==========
def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)

    if len(sys.argv) < 2:
        info_hash = get_input_with_timeout("请输入种子info hash: ")
    else:
        info_hash = sys.argv[1].strip()

    if not info_hash:
        log("info hash为空", "ERROR")
        sys.exit(1)

    base_url, user, pwd, _ = load_config()
    sid = login_qb(base_url, user, pwd)
    _, save_path, _ = get_torrent_info(base_url, sid, info_hash)
    is_bdmv_task = has_bdmv_folder(save_path) if save_path else False

    if is_bdmv_task:
        add_to_queue(info_hash)
        lock_fd = acquire_lock()
        if not lock_fd:
            log("检测到已有领头羊进程在运行，本进程仅提交BDMV任务后退出", "INFO")
            sys.exit(0)
        
        log("成为领头羊进程，开始处理BDMV队列任务", "INFO")
        try:
            while True:
                current_task = get_next_task()
                if not current_task:
                    log("BDMV队列为空，所有任务处理完成", "INFO")
                    break
                process_single_task(current_task, is_bdmv_task=True)
        finally:
            release_lock(lock_fd)
    else:
        log("当前任务为非BDMV类型，跳过队列直接实时处理", "INFO")
        process_single_task(info_hash, is_bdmv_task=False)

if __name__ == "__main__":
    main()
