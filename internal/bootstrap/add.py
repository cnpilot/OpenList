import requests
import re
import time
import json
import os
import logging

# ========== 日志文件路径配置 ==========
log_dir = '/home/boxbox/'
os.makedirs(log_dir, exist_ok=True)
logging.basicConfig(
    filename=os.path.join(log_dir, 'failed_add_torrents.log'),
    level=logging.ERROR,
    format='%(asctime)s - %(levelname)s - %(message)s'
)


class QBittorrentClient:
    def __init__(self, qb_config_path, site_config_path):
        self.qb_config = self.load_config(qb_config_path)
        self.site_config = self.load_config(site_config_path)
        self.download_base_path = self.qb_config.get('download_base_path', '/home/boxbox/qbittorrent/download')
        self.metadata_save_path = self.qb_config.get('metadata_save_path', '/home/boxbox/title')
        self.session = self.login()

    def load_config(self, config_path):
        try:
            with open(config_path, 'r', encoding='utf-8') as f:
                return json.load(f)
        except Exception as e:
            logging.error(f"配置文件加载失败: {config_path}, 错误: {e}")
            raise

    def login(self):
        try:
            response = requests.post(
                f"{self.qb_config['address']}/api/v2/auth/login",
                data={
                    'username': self.qb_config['username'],
                    'password': self.qb_config['password']
                },
                timeout=10
            )
            response.raise_for_status()
            print("qBittorrent登录成功")
            return response.cookies
        except Exception as e:
            logging.error(f"qBittorrent登录失败: {e}")
            raise

    def ensure_directory_permissions(self, path):
        if not os.path.exists(path):
            os.makedirs(path, exist_ok=True)
            target_uid = int(os.getenv("TARGET_UID", os.stat('/home/boxbox').st_uid))
            target_gid = int(os.getenv("TARGET_GID", os.stat('/home/boxbox').st_gid))
            os.chown(path, target_uid, target_gid)
            os.chmod(path, 0o755)
            print(f"已创建并配置目录权限: {path}")

    def calculate_total_size(self):
        try:
            response = requests.get(
                f"{self.qb_config['address']}/api/v2/torrents/info",
                cookies=self.session,
                timeout=10
            )
            response.raise_for_status()
            torrents = response.json()
            total_bytes = sum(torrent.get('size', 0) for torrent in torrents)
            return round(total_bytes / (1024 ** 3), 2)
        except Exception as e:
            logging.error(f"计算总大小失败: {e}")
            return 0.0

    def add_torrent_from_link(self, torrent_link, save_path, tags=None, retries=3):
        data = {
            'urls': torrent_link,
            'savepath': save_path,
            'category': 'default',
            'skip_checking': 'false',
            'paused': 'false'
        }
        if tags:
            data['tags'] = tags

        headers = {
            'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.132 Safari/537.36'
        }

        for attempt in range(retries):
            try:
                response = requests.post(
                    f"{self.qb_config['address']}/api/v2/torrents/add",
                    data=data,
                    cookies=self.session,
                    headers=headers,
                    timeout=15
                )
                response.raise_for_status()
                print(f"种子添加成功: {torrent_link}")
                time.sleep(2)
                return True
            except Exception as e:
                print(f"添加失败(尝试 {attempt+1}/{retries}): {e}")
                if attempt < retries - 1:
                    time.sleep(2)
                else:
                    logging.error(f"种子添加最终失败: {torrent_link}, 错误: {e}")
                    raise
        return False

    def extract_id_from_url(self, url):
        try:
            if "totheglory.im" in url:
                id_string = url.strip().split('/')[-1]
                print(f"提取TTG种子ID: {id_string}")
                return id_string
            id_string = url.split('id=')[1].split('&')[0]
            print(f"提取种子ID: {id_string}")
            return id_string
        except Exception as e:
            logging.error(f"ID提取失败: {url}, 错误: {e}")
            raise

    def get_imdb_id_and_titles_from_url(self, url, cookie):
        headers = {
            "Cookie": cookie,
            "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.132 Safari/537.36",
        }

        try:
            response = requests.get(url, headers=headers, timeout=30, allow_redirects=True)
            response.raise_for_status()
            html = response.text

            imdb_id = "No_IMDb"
            imdb_match = re.search(r'title/(tt\d+)', html)
            if imdb_match:
                imdb_id = imdb_match.group(1)
            else:
                data_imdb_match = re.search(r'data-imdbid=(\'|")(\d+)\1', html)
                if data_imdb_match:
                    imdb_id = f"tt{data_imdb_match.group(2)}"

            douban_id = "No_Douban"
            douban_link_match = re.search(r'douban\.com/subject/(\d+)', html)
            if douban_link_match:
                douban_id = f"豆瓣号-{douban_link_match.group(1)}"
            else:
                data_douban_match = re.search(r'data-doubanid=(\'|")?(\d+)\1?', html)
                if data_douban_match:
                    douban_id = f"豆瓣号-{data_douban_match.group(2)}"

            if "totheglory.im" in url:
                h1_match = re.search(r'<h1>(.*?)</h1>', html, re.DOTALL)
                if h1_match:
                    full_title = h1_match.group(1).strip().replace('&quot;', '"')
                    if '[' in full_title:
                        title = full_title.split('[', 1)[0].strip()
                        subtitle = '[' + full_title.split('[', 1)[1].strip()
                    else:
                        title = full_title
                        subtitle = "无"
                else:
                    title = "未找到标题"
                    subtitle = "未找到副标题"
            else:
                title_match = re.search(r'<title>(.*?)</title>', html, re.DOTALL)
                title = "未找到标题"
                if title_match:
                    title = re.sub(r' - Powered by NexusPHP$', '', title_match.group(1).strip())
                    title = title.replace('&quot;', '"')

                subtitle = "无副标题"
                subtitle_match = re.search(
                    r'<div class="font-bold leading-6">副标题</div>\s*<div class="font-bold leading-6">(.*?)</div>',
                    html, re.DOTALL
                )
                if not subtitle_match:
                    subtitle_match = re.search(
                        r'<td class="rowhead nowrap".*?>副标题</td>\s*<td class="rowfollow".*?>(.*?)</td>',
                        html, re.DOTALL
                    )
                if subtitle_match:
                    subtitle = subtitle_match.group(1).strip()
                subtitle = re.sub(r'<[^>]+>', '', subtitle)

            title = re.sub(r'\s+', ' ', title).strip()
            subtitle = re.sub(r'\s+', ' ', subtitle).strip()

            print(f"解析结果: IMDb={imdb_id}, 标题={title}, 副标题={subtitle}, 豆瓣ID={douban_id}")
            return imdb_id, title, subtitle, douban_id

        except Exception as e:
            logging.error(f"详情页解析失败: {url}, 错误: {e}")
            return "No_IMDb", "解析失败", "解析失败", "No_Douban"

    def save_metadata_details(self, id_string, site_name, title, subtitle, imdb_id, douban_id,
                             primary_path=None, secondary_path=None, is_cleaned=False):
        try:
            cleaned_subtitle = subtitle.strip()
            cleaned_subtitle = re.sub(r'[<>:"/\\|?*]', '', cleaned_subtitle)
            cleaned_subtitle = re.sub(r'\s+', ' ', cleaned_subtitle).replace(' ', '_')

            max_subtitle_length = 70
            if len(cleaned_subtitle) > max_subtitle_length:
                cleaned_subtitle = cleaned_subtitle[:max_subtitle_length]
            file_name = f"{cleaned_subtitle}.txt"

            def write_file(path):
                self.ensure_directory_permissions(path)
                path_length = len(path) + 1
                max_file_name_length = 251 - path_length
                if len(file_name) > max_file_name_length:
                    remaining_length = max_file_name_length - 4
                    cleaned_subtitle_adj = cleaned_subtitle[:remaining_length]
                    file_name_adj = cleaned_subtitle_adj + ".txt"
                else:
                    file_name_adj = file_name

                file_path = os.path.join(path, file_name_adj)
                with open(file_path, 'w', encoding='utf-8') as f:
                    f.write(f"Title: {title}\n")
                    f.write(f"Subtitle: {subtitle}\n")
                    if imdb_id != "No_IMDb":
                        f.write(f"IMDb链接: https://www.imdb.com/title/{imdb_id}\n")
                    else:
                        f.write(f"IMDb链接: 无\n")
                    if douban_id != "No_Douban":
                        douban_num = douban_id.replace("豆瓣号-", "")
                        f.write(f"豆瓣链接: https://douban.com/subject/{douban_num}\n")
                    else:
                        f.write(f"豆瓣链接: 无\n")
                    f.write(f"Site: {site_name}\n")
                    f.write(f"Torrent ID: {id_string}\n")
                print(f"已保存元数据: {file_path}")

            if primary_path:
                write_file(primary_path)
            if secondary_path:
                write_file(secondary_path)

        except Exception as e:
            log_type = "清理后的" if is_cleaned else ""
            logging.error(f"保存{log_type}元数据失败: {e}, 站点={site_name}, ID={id_string}")
            print(f"保存{log_type}元数据失败: {e}")

    def generate_path_prefix(self, title, site_name):
        # 取消分辨率/编码分类，统一前缀仅为 站点名_
        return f"{site_name}_"

    def get_site_config(self, url):
        for keyword, config in self.site_config['site_keywords'].items():
            if keyword in url:
                return config
        print(f"未找到匹配站点，使用默认配置")
        return {'name': 'Ubits', 'cookie': 'default', 'passkey': 'default', 'hostname': 'ubits.club'}

    def generate_download_url(self, id_string, site_config):
        hostname = site_config['hostname']
        cookie = site_config['cookie']

        if "hdhome.org" in hostname:
            url = f"https://{hostname}/details.php?id={id_string}"
            headers = {
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.132 Safari/537.36",
            }
            response = requests.get(url, headers=headers, timeout=10)
            match = re.search(r'<a href="(http://hdhome\.org/download\.php\?id=\d+&downhash=[^"]+)">', response.text)
            return match.group(1) if match else ""

        elif "hdsky.me" in hostname:
            url = f"https://{hostname}/details.php?id={id_string}"
            headers = {
                "Cookie": cookie,
                "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/80.0.3987.132 Safari/537.36",
            }
            response = requests.get(url, headers=headers, timeout=10)
            match = re.search(r'<a href="(https://hdsky\.me/download\.php\?id=\d+&passkey=[^"]+&sign=[^"]+)">', response.text)
            return match.group(1) if match else ""

        elif "hhanclub.net" in hostname:
            return f"https://{hostname}/download.php?id={id_string}&passkey={site_config['passkey']}"

        elif "totheglory.im" in hostname:
            return f"https://{hostname}/dl/{id_string}/{site_config['passkey']}"

        else:
            return f"https://{hostname}/download.php?id={id_string}&passkey={site_config['passkey']}"

    def process_links(self, links_file):
        if not os.path.exists(links_file):
            print(f"链接文件不存在: {links_file}")
            return

        with open(links_file, 'r') as f:
            links = [line.strip() for line in f if line.strip()]

        total_links = len(links)
        print(f"共发现 {total_links} 个种子链接")

        for count, url in enumerate(links, 1):
            try:
                print(f"\n===== 处理第 {count}/{total_links} 个链接 =====")
                id_string = self.extract_id_from_url(url)
                site_config = self.get_site_config(url)

                if "totheglory.im" in site_config['hostname']:
                    details_url = f"https://{site_config['hostname']}/t/{id_string}/"
                else:
                    details_url = f"https://{site_config['hostname']}/details.php?id={id_string}"

                imdb_id, title, subtitle, douban_id = self.get_imdb_id_and_titles_from_url(details_url, site_config['cookie'])

                filter_keyword = "-HDSPad"
                full_title = f"{title} {subtitle}".lower()
                if filter_keyword in full_title:
                    print(f"检测到 {filter_keyword}，跳过此种子：{title}")
                    continue

                path_prefix = self.generate_path_prefix(title, site_config['name'])

                # HHclub 专用：豆瓣号
                if site_config['name'].lower() == "hhclub":
                    if douban_id != "No_Douban":
                        db_id = douban_id.replace("豆瓣号-", "豆瓣号_")
                        qb_save_path = os.path.join(self.download_base_path, f"{path_prefix}{id_string}_{db_id}")
                    else:
                        qb_save_path = os.path.join(self.download_base_path, f"{path_prefix}{id_string}_NoDouban")
                else:
                    qb_save_path = os.path.join(self.download_base_path, f"{path_prefix}{id_string}_{imdb_id}")

                self.save_metadata_details(
                    id_string, site_config['name'], title, subtitle, imdb_id, douban_id,
                    primary_path=self.metadata_save_path,
                    is_cleaned=False
                )
                self.save_metadata_details(
                    id_string, site_config['name'], title, subtitle, imdb_id, douban_id,
                    primary_path='/home/boxbox/Subtitle',
                    secondary_path=qb_save_path,
                    is_cleaned=True
                )

                download_url = self.generate_download_url(id_string, site_config)
                if not download_url:
                    raise ValueError("无法生成有效的下载链接")

                tags = imdb_id if imdb_id != "No_IMDb" else None
                if self.add_torrent_from_link(download_url, qb_save_path, tags):
                    total_size = self.calculate_total_size()
                    print(f"当前种子总大小: {total_size} GB")

                if count < total_links:
                    print(f"等待5秒后处理下一个...")
                    time.sleep(5)
                else:
                    print(f"所有 {total_links} 个种子链接处理完成")

            except Exception as e:
                logging.error(f"处理链接 {url} 失败: {e}")
                print(f"处理链接失败: {e}")
                if count == total_links:
                    print(f"所有 {total_links} 个种子链接处理完成（部分可能失败）")
                continue


if __name__ == "__main__":
    qb_config_path = '/home/boxbox/box_qb_config.json'
    site_config_path = '/home/boxbox/site_config.json'

    os.environ["TARGET_UID"] = str(os.stat('/home/boxbox').st_uid)
    os.environ["TARGET_GID"] = str(os.stat('/home/boxbox').st_gid)

    client = QBittorrentClient(qb_config_path, site_config_path)
    client.process_links('/home/boxbox/links.txt')
