#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
BDMV/非BDMV种子处理脚本 - BDMV直接跳过极简版
归档路径修改为 /home/boxbox/welldone
=============================================
改动：移除BDMV队列+文件锁，BDMV原盘直接跳过全部处理，无任何文件操作
冗余无用代码清理，修复无效导入、未使用函数
归档目标目录由finish改为welldone
>>> 额外修改：QB登录逻辑对齐第一个批量脚本，使用完整CookieJar
>>> 修复BUG：is_bdmv_valid路径拼接错误导致BDMV检测失效
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
    log("捕获终止信号，优雅退出", "WARN")
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
    timer = Timer(timeout, lambda: sys.exit(1))
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
    # 对齐批量脚本：返回完整CookieJar
    try:
        resp = requests.post(
            f"{base_url}/api/v2/auth/login",
            data={"username": user, "password": pwd},
            timeout=15
        )
        resp.raise_for_status()
        log("qBittorrent登录成功")
        return resp.cookies
    except Exception as e:
        log(f"QB登录失败: {e}", "ERROR")
        sys.exit(1)
def get_torrent_info(base_url, cookies, info_hash):
    # 使用cookies参数传入CookieJar，不再手动拼接SID
    try:
        resp = requests.get(
            f"{base_url}/api/v2/torrents/info",
            params={"hashes": info_hash},
            cookies=cookies,
            timeout=15
        )
        resp.raise_for_status()
        data = resp.json()
        if not data:
            log(f"未查询到种子: {info_hash}", "ERROR")
            return None, None, None
        item = data[0]
        return item["name"], item["save_path"], item.get("tags", "")
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
    try:
        for f in os.listdir(stream_dir):
            f_path = os.path.join(stream_dir, f)
            if os.path.isfile(f_path) and f.lower().endswith(".m2ts"):
                if os.path.getsize(f_path) > 1 * 1024 * 1024:
                    return True
    except PermissionError:
        log(f"权限不足，无法读取 {stream_dir}", "WARN")
    except Exception as e:
        log(f"扫描STREAM目录异常 {stream_dir}: {e}", "WARN")
    return False
def find_all_bdmv_dirs(root_path):
    bdmv_parent_set = set()
    stack = [root_path]
    while stack:
        current = stack.pop()
        try:
            subitems = os.listdir(current)
        except PermissionError:
            log(f"无权访问目录 {current}", "WARN")
            continue
        for name in subitems:
            fullpath = os.path.join(current, name)
            if os.path.isdir(fullpath):
                if name == "BDMV":
                    if is_bdmv_valid(fullpath):
                        parent = os.path.dirname(fullpath)
                        bdmv_parent_set.add(parent)
                        log(f"识别到有效BDMV，父目录: {parent}")
                else:
                    stack.append(fullpath)
    return list(bdmv_parent_set)
def has_bdmv_folder(save_path):
    return len(find_all_bdmv_dirs(save_path)) > 0
def has_iso_file(save_path):
    for root, dirs, files in os.walk(save_path):
        for fn in files:
            if fn.lower().endswith(".iso"):
                return True
    return False
# ========== 非BDMV处理逻辑 ==========
def is_remux(name):
    return "remux" in name.lower()
def is_web_dl(name):
    return "web-dl" in name.lower() or "webdl" in name.lower()
def process_non_bdmv(save_path, torrent_name, tags, tmdb_key):
    log("未检测BDMV/ISO，开始普通影片处理")
    src_item = os.path.join(save_path, torrent_name)
    parent_dir = os.path.dirname(src_item)
    if parent_dir.rstrip("/") == DEFAULT_DOWNLOAD_ROOT.rstrip("/"):
        work_src = src_item
        log(f"模式：根目录种子，源路径 {work_src}")
    else:
        work_src = parent_dir
        log(f"模式：子目录种子，源路径 {work_src}")
    target_base = os.path.join("/home/boxbox/welldone", os.path.basename(work_src))
    # 第一步 torcp
    torcp_out = os.path.join("/home/boxbox/emby_tmp", torrent_name)
    torcp_cmd = [
        "torcp",
        "-i", src_item,
        "-o", torcp_out,
        "-tmdb", tmdb_key,
        "-tags", tags,
        "-s"
    ]
    log(f"执行torcp: {' '.join(torcp_cmd)}")
    try:
        os.makedirs(os.path.dirname(LOG_TORCP_PATH), exist_ok=True)
        with open(LOG_TORCP_PATH, "a", encoding="utf-8") as fp:
            subprocess.run(torcp_cmd, stdout=fp, stderr=subprocess.STDOUT, check=False)
    except Exception as e:
        log(f"torcp执行异常: {e}", "WARN")
    time.sleep(2)
    # 第二步 rclone move到媒体库
    if is_web_dl(torrent_name):
        media_dst = "/home/boxbox/emby_lib/web-dl/"
    elif is_remux(torrent_name):
        media_dst = "/home/boxbox/emby_lib/remux/"
    else:
        media_dst = "/home/boxbox/emby_lib/encode/"
    rclone_move_cmd = [
        "rclone", "move",
        torcp_out,
        media_dst,
        "-v", "--stats", "20s",
        "--transfers", "3",
        "--drive-chunk-size", "32M",
        "--delete-empty-src-dirs"
    ]
    log(f"执行rclone迁移媒体库: {' '.join(rclone_move_cmd)}")
    try:
        os.makedirs(os.path.dirname(LOG_RCLONE_PATH), exist_ok=True)
        with open(LOG_RCLONE_PATH, "a", encoding="utf-8") as fp:
            subprocess.run(rclone_move_cmd, stdout=fp, stderr=subprocess.STDOUT, check=False)
        subprocess.run(["find", "/home/boxbox/emby_tmp", "-type", "d", "-empty", "-delete"], check=False)
    except Exception as e:
        log(f"媒体库rclone异常: {e}", "WARN")
    time.sleep(2)
    # 第三步 归档源文件到welldone
    try:
        os.makedirs("/home/boxbox/welldone", exist_ok=True)
        archive_cmd = [
            "rclone", "move",
            work_src,
            target_base,
            "-v", "--stats", "20s",
            "--transfers", "2",
            "--delete-empty-src-dirs"
        ]
        log(f"归档源文件到welldone: {' '.join(archive_cmd)}")
        with open(LOG_FINISH_MOVE, "a", encoding="utf-8") as fp:
            subprocess.run(archive_cmd, stdout=fp, stderr=subprocess.STDOUT, check=False)
        log(f"全部流程完成，源归档至 {target_base}")
    except Exception as e:
        log(f"归档rclone异常: {e}", "ERROR")
def main():
    signal.signal(signal.SIGINT, signal_handler)
    signal.signal(signal.SIGTERM, signal_handler)
    if len(sys.argv) < 2:
        hash_in = get_input_with_timeout("请输入种子info_hash：")
    else:
        hash_in = sys.argv[1].strip()
    if not hash_in:
        log("info_hash为空，退出", "ERROR")
        sys.exit(1)
    addr, user, pwd, tmdb_api = load_config()
    cookies = login_qb(addr, user, pwd)
    t_name, t_savepath, t_tags = get_torrent_info(addr, cookies, hash_in)
    if not t_name or not t_savepath:
        sys.exit(1)
    log(f"种子名称:{t_name} 保存路径:{t_savepath}")
    if has_iso_file(t_savepath):
        log("检测到ISO镜像，跳过处理", "INFO")
        return
    if has_bdmv_folder(t_savepath):
        log("检测BDMV原盘，跳过处理", "INFO")
        return
    process_non_bdmv(t_savepath, t_name, t_tags, tmdb_api)
if __name__ == "__main__":
    main()
