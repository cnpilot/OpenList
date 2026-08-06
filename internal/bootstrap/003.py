#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BDMV/非BDMV种子处理脚本 - BDMV直接跳过极简版
=============================================
改动：移除BDMV队列+文件锁，BDMV原盘直接跳过全部处理，无任何文件操作
冗余无用代码清理，修复无效导入、未使用函数
=============================================
"""

import sys
import os
import json
import requests
import subprocess
import time
import signal
from datetime import datetime
from threading import Timer

# ========== 核心配置 ==========
CONFIG_FILE = "/home/boxbox/box_qb_config.json"
LOG_FILE = "/home/boxbox/logs_bdmv/main.log"
LOG_TORCP_PATH = "/home/boxbox/logs_bdmv/torcp_process.log"
LOG_RCLONE_PATH = "/home/boxbox/logs_bdmv/rclone_move.log"
LOG_FINISH_MOVE = "/home/boxbox/logs_bdmv/rclone_finish_move.log"
DEFAULT_DOWNLOAD_ROOT = "/home/boxbox/qbittorrent/download"

# ========== 工具函数 ==========
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

# ========== BDMV/ISO检测 ==========
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

# ========== 非BDMV处理逻辑（完全原版无改动） ==========
def is_remux(name):
    return "remux" in name.lower()

def is_web_dl(name):
    return "web-dl" in name.lower()

def process_non_bdmv_folders(save_path, name, tags, tmdb_api_key):
    log("未找到 BDMV 文件夹，执行非原盘媒体处理流程")

    inner_path = os.path.join(save_path, name)
    parent_dir = os.path.dirname(inner_path)

    # 两种目录结构自动适配
    if parent_dir.rstrip("/") == DEFAULT_DOWNLOAD_ROOT.rstrip("/"):
        move_src = inner_path
        log(f"[自动识别] 直接下载模式，移动种子目录: {os.path.basename(move_src)}")
    else:
        move_src = parent_dir
        log(f"[自动识别] 脚本下载模式，移动站点目录: {os.path.basename(move_src)}")

    move_folder_name = os.path.basename(move_src)
    target_finish = os.path.join("/home/boxbox/finish", move_folder_name)

    # 第一步 torcp刮削命名
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

    # 第二步 rclone媒体库分类转存
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

    # 第三步 源目录归档移动至finish
    try:
        os.makedirs("/home/boxbox/finish", exist_ok=True)
        move_cmd = [
            "rclone", "move",
            move_src, target_finish,
            "-v", "--transfers", "2",
            "--stats", "300s",
            "--delete-empty-src-dirs",
            f"--log-file={LOG_FINISH_MOVE}"
        ]
        
        log(f"【执行命令3-rclone-目录归档】 {' '.join(move_cmd)}")
        subprocess.run(move_cmd, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=False)
        log(f"整套流程完毕 | 源：{move_src} | 目标：{target_finish}", "INFO")
    except Exception as e:
        log(f"归档移动异常: {e}", "ERROR")

# ========== 单任务处理入口 ==========
def process_single_task(info_hash):
    try:
        log(f"开始处理任务: {info_hash}")
        base_url, user, pwd, tmdb_api_key = load_config()

        sid = login_qb(base_url, user, pwd)
        if not sid:
            log(f"QB登录失败，跳过任务: {info_hash}", "ERROR")
            return

        torrent_name, save_path, tags = get_torrent_info(base_url, sid, info_hash)
        if not torrent_name or not save_path:
            log(f"获取种子信息失败，跳过任务: {info_hash}", "ERROR")
            return

        # 规则1：存在ISO直接全部跳过
        if has_iso_file(save_path):
            log("检测到ISO镜像文件，跳过全部处理", "INFO")
            return

        # 规则2：BDMV原盘直接全部跳过
        if has_bdmv_folder(save_path):
            log("识别为BDMV蓝光原盘，不作任何文件处理，直接跳过", "INFO")
            return

        # 普通影片正常处理
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

    # 直接处理，无队列、无锁
    process_single_task(info_hash)

if __name__ == "__main__":
    main()
