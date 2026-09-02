# -*- encoding: utf-8 -*-

"""
Author: Hmily
GitHub: https://github.com/ihmily
Date: 2023-07-15 23:15:00
Update: 2025-10-23 18:28:00
Copyright (c) 2023-2025 by Hmily, All Rights Reserved.
Function: Get live stream data.
"""

import urllib.parse
import re
import json
from .utils import trace_error_decorator
from .room import get_sec_user_id, get_unique_id, UnsupportedUrlError
from .http_clients.async_http import async_req
from .ab_sign import ab_sign
from .logger import logger

OptionalStr = str | None

_XHS_USER_AGENT = "ios/7.830 (ios 17.0; ; iPhone 15 (A2846/A3089/A3090/A3092))"
_XHS_COMMON_PARAMS = "platform=iOS&sid=session.1722166379345546829388"
_XHS_REFERER = "https://app.xhs.cn/"


def _parse_xhs_initial_state(html_str: str) -> dict | None:
    match = re.search(
        r"window\.__INITIAL_STATE__\s*=\s*(.*?)</script>",
        html_str,
        re.DOTALL,
    )
    if not match:
        return None
    payload = match.group(1).strip().rstrip(";")
    try:
        return json.loads(payload.replace("undefined", "null"))
    except json.JSONDecodeError:
        return None


def _xhs_deeplink_params(room_info: dict) -> dict[str, list[str]]:
    deeplink = room_info.get("deeplink") or ""
    try:
        return urllib.parse.parse_qs(urllib.parse.urlparse(deeplink).query)
    except (TypeError, ValueError):
        return {}


def _xhs_stream_urls(
    room_info: dict,
    video_quality: str | None,
) -> tuple[str | None, str | None, str | None, str | None]:
    """返回 (flv, hls, codec, quality)，优先使用 H.264 和所选画质。"""
    pull_config = room_info.get("pullConfig")
    if isinstance(pull_config, str):
        try:
            pull_config = json.loads(pull_config)
        except json.JSONDecodeError:
            pull_config = None

    desired_quality = {
        "OD": "HD",
        "BD": "HD",
        "UHD": "HD",
        "HD": "HD",
        "SD": "SD",
        "LD": "LD",
    }.get(str(video_quality or "OD").upper(), "HD")

    if isinstance(pull_config, dict):
        for codec_key in ("h264", "h265"):
            raw_streams = pull_config.get(codec_key) or []
            if not isinstance(raw_streams, list) or not raw_streams:
                continue

            streams = [item for item in raw_streams if isinstance(item, dict)]
            available_qualities = [
                str(item.get("quality_type") or "HD").upper() for item in streams
            ]
            quality_order = [desired_quality, "HD", "SD", "LD"]
            quality_order.extend(q for q in available_qualities if q not in quality_order)
            for selected_quality in quality_order:
                flv_url = None
                m3u8_url = None
                for item in streams:
                    quality = str(item.get("quality_type") or "HD").upper()
                    if quality != selected_quality:
                        continue
                    stream_url = item.get("master_url") or item.get("url")
                    if not isinstance(stream_url, str) or not stream_url:
                        continue
                    path = urllib.parse.urlsplit(stream_url).path.lower()
                    if path.endswith(".flv") and flv_url is None:
                        flv_url = stream_url
                    elif path.endswith(".m3u8") and m3u8_url is None:
                        m3u8_url = stream_url
                if flv_url or m3u8_url:
                    return flv_url, m3u8_url, codec_key, selected_quality

    # 兼容旧页面：流地址只出现在 deeplink 查询参数中。
    params = _xhs_deeplink_params(room_info)
    flv_url = (params.get("flvUrl") or params.get("flv_url") or [None])[0]
    m3u8_url = (params.get("hlsUrl") or params.get("m3u8Url") or [None])[0]
    if flv_url and not m3u8_url:
        m3u8_url = re.sub(r"\.flv(?=\?|$)", ".m3u8", flv_url, flags=re.IGNORECASE)
    return flv_url, m3u8_url, "h264", None


async def get_xhs_stream_url(
    url: str,
    video_quality: str | None = None,
    proxy_addr: OptionalStr = None,
    cookies: OptionalStr = None,
) -> dict:
    """解析小红书当前直播间；不提供基于主播主页的长期开播发现。"""
    result = {
        "anchor_name": "",
        "is_live": False,
        "fetch_ok": False,
    }
    headers = {
        "User-Agent": _XHS_USER_AGENT,
        "xy-common-params": _XHS_COMMON_PARAMS,
        "Referer": _XHS_REFERER,
    }
    if cookies:
        headers["Cookie"] = cookies

    try:
        resolved_url = url
        if "xhslink.com" in urllib.parse.urlparse(url).netloc.lower():
            redirect_url = await async_req(
                url,
                proxy_addr=proxy_addr,
                headers=headers,
                redirect_url=True,
            )
            if isinstance(redirect_url, str) and redirect_url:
                resolved_url = redirect_url

        resolved = urllib.parse.urlsplit(resolved_url)
        if (resolved.hostname or "").rstrip(".").lower() not in {
            "xiaohongshu.com",
            "www.xiaohongshu.com",
        }:
            raise ValueError("分享链接未跳转到小红书直播间")
        room_match = re.fullmatch(
            r"/livestream/(?:[A-Za-z0-9_-]+/)?(\d+)/?",
            resolved.path,
        )
        if not room_match:
            raise ValueError("分享链接未跳转到有效的小红书直播间地址")

        room_id = room_match.group(1)
        room_url = f"https://www.xiaohongshu.com/livestream/{room_id}"
        html_str = await async_req(room_url, proxy_addr=proxy_addr, headers=headers)
        if not isinstance(html_str, str) or not html_str:
            raise RuntimeError("直播间页面返回为空")

        state = _parse_xhs_initial_state(html_str)
        if state is None:
            raise RuntimeError("直播间页面缺少 __INITIAL_STATE__，可能触发登录限制或页面已更新")

        live_stream = state.get("liveStream") or {}
        room_info = (live_stream.get("roomData") or {}).get("roomInfo") or {}
        params = _xhs_deeplink_params(room_info)
        anchor_name = (params.get("host_nickname") or [""])[0]
        if not anchor_name:
            anchor_name = (
                room_info.get("hostName")
                or room_info.get("nickname")
                or room_info.get("anchorName")
                or ""
            )

        result.update({
            "anchor_name": anchor_name,
            "fetch_ok": True,
            "room_id": room_id,
            "title": room_info.get("roomTitle") or "",
            "ffmpeg_user_agent": _XHS_USER_AGENT,
            "ffmpeg_referer": _XHS_REFERER,
        })
        title = result["title"]
        if live_stream.get("liveStatus") != "success" or (title and "回放" in title):
            return result

        flv_url, m3u8_url, codec, quality = _xhs_stream_urls(room_info, video_quality)
        real_url = flv_url or m3u8_url
        if not real_url:
            raise RuntimeError("直播间已开播，但页面中没有可用的 FLV/HLS 地址")

        result.update({
            "is_live": True,
            "flv_url": flv_url,
            "m3u8_url": m3u8_url,
            "record_url": real_url,
            "codec": codec,
            "quality": quality,
        })
    except Exception as exc:
        result["fetch_ok"] = False
        result["error"] = str(exc)
        logger.warning(f"小红书直播解析失败 ({url}): {exc}")
    return result


async def get_douyin_web_stream_data(url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None):
    headers = {
        'cookie': 'ttwid=1%7C2iDIYVmjzMcpZ20fcaFde0VghXAA3NaNXE_SLR68IyE%7C1761045455'
                  '%7Cab35197d5cfb21df6cbb2fa7ef1c9262206b062c315b9d04da746d0b37dfbc7d',
        'referer': 'https://live.douyin.com/335354047186',
        'user-agent': 'Mozilla/5.0 (Windows NT 10.0; WOW64) AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/116.0.5845.97 Safari/537.36 Core/1.116.567.400 QQBrowser/19.7.6764.400',
    }
    if cookies:
        headers['cookie'] = cookies

    try:
        web_rid = url.split('?')[0].split('live.douyin.com/')[-1]
        params = {
            "aid": "6383",
            "app_name": "douyin_web",
            "live_id": "1",
            "device_platform": "web",
            "language": "zh-CN",
            "browser_language": "zh-CN",
            "browser_platform": "Win32",
            "browser_name": "Chrome",
            "browser_version": "116.0.0.0",
            "web_rid": web_rid,
            'msToken': '',
        }

        api = f'https://live.douyin.com/webcast/room/web/enter/?{urllib.parse.urlencode(params)}'
        a_bogus = ab_sign(urllib.parse.urlparse(api).query, headers['user-agent'])
        api += "&a_bogus=" + a_bogus
        try:
            json_str = await async_req(url=api, proxy_addr=proxy_addr, headers=headers)
            if not json_str:
                raise Exception("it triggered risk control")
            json_data = json.loads(json_str)['data']
            if not json_data['data']:
                raise Exception(f"{url} VR live is not supported")
            room_data = json_data['data'][0]
            room_data['anchor_name'] = json_data['user']['nickname']
        except Exception as e:
            raise Exception(f"Douyin web data fetch error, because {e}.")

        if room_data['status'] == 2:
            if 'stream_url' not in room_data:
                raise RuntimeError(
                    "The live streaming type or gameplay is not supported on the computer side yet, please use the "
                    "app to share the link for recording."
                )
            live_core_sdk_data = room_data['stream_url']['live_core_sdk_data']
            pull_datas = room_data['stream_url']['pull_datas']
            if live_core_sdk_data:
                if pull_datas:
                    key = list(pull_datas.keys())[0]
                    json_str = pull_datas[key]['stream_data']
                else:
                    json_str = live_core_sdk_data['pull_data']['stream_data']
                json_data = json.loads(json_str)
                if 'origin' in json_data['data']:
                    stream_data = live_core_sdk_data['pull_data']['stream_data']
                    origin_data = json.loads(stream_data)['data']['origin']['main']
                    sdk_params = json.loads(origin_data['sdk_params'])
                    origin_hls_codec = sdk_params.get('VCodec') or ''

                    origin_url_list = json_data['data']['origin']['main']
                    origin_m3u8 = {'ORIGIN': origin_url_list["hls"] + '&codec=' + origin_hls_codec}
                    origin_flv = {'ORIGIN': origin_url_list["flv"] + '&codec=' + origin_hls_codec}
                    hls_pull_url_map = room_data['stream_url']['hls_pull_url_map']
                    flv_pull_url = room_data['stream_url']['flv_pull_url']
                    room_data['stream_url']['hls_pull_url_map'] = {**origin_m3u8, **hls_pull_url_map}
                    room_data['stream_url']['flv_pull_url'] = {**origin_flv, **flv_pull_url}
    except Exception as e:
        logger.error(f"抓取失败: {e} (行 {e.__traceback__.tb_lineno})")
        room_data = {'anchor_name': ""}
    return room_data


@trace_error_decorator
async def get_douyin_app_stream_data(url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None) -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) '
                      'Chrome/141.0.0.0 Safari/537.36 Edg/141.0.0.0',
        'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
        'Referer': 'https://live.douyin.com/',
        'Cookie': 'ttwid=1%7CB1qls3GdnZhUov9o2NxOMxxYS2ff6OSvEWbv0ytbES4%7C1680522049%7C280d802d6d478e3e78d0c807f7c487e7ffec0ae4e5fdd6a0fe74c3c6af149511; my_rd=1; passport_csrf_token=3ab34460fa656183fccfb904b16ff742; passport_csrf_token_default=3ab34460fa656183fccfb904b16ff742; d_ticket=9f562383ac0547d0b561904513229d76c9c21; n_mh=hvnJEQ4Q5eiH74-84kTFUyv4VK8xtSrpRZG1AhCeFNI; store-region=cn-fj; store-region-src=uid; LOGIN_STATUS=1; __security_server_data_status=1; FORCE_LOGIN=%7B%22videoConsumedRemainSeconds%22%3A180%7D; pwa2=%223%7C0%7C3%7C0%22; download_guide=%223%2F20230729%2F0%22; volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Afalse%2C%22volume%22%3A0.6%7D; strategyABtestKey=%221690824679.923%22; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A1536%2C%5C%22screen_height%5C%22%3A864%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A8%2C%5C%22device_memory%5C%22%3A8%2C%5C%22downlink%5C%22%3A10%2C%5C%22effective_type%5C%22%3A%5C%224g%5C%22%2C%5C%22round_trip_time%5C%22%3A150%7D%22; VIDEO_FILTER_MEMO_SELECT=%7B%22expireTime%22%3A1691443863751%2C%22type%22%3Anull%7D; home_can_add_dy_2_desktop=%221%22; __live_version__=%221.1.1.2169%22; device_web_cpu_core=8; device_web_memory_size=8; xgplayer_user_id=346045893336; csrf_session_id=2e00356b5cd8544d17a0e66484946f28; odin_tt=724eb4dd23bc6ffaed9a1571ac4c757ef597768a70c75fef695b95845b7ffcd8b1524278c2ac31c2587996d058e03414595f0a4e856c53bd0d5e5f56dc6d82e24004dc77773e6b83ced6f80f1bb70627; __ac_nonce=064caded4009deafd8b89; __ac_signature=_02B4Z6wo00f01HLUuwwAAIDBh6tRkVLvBQBy9L-AAHiHf7; ttcid=2e9619ebbb8449eaa3d5a42d8ce88ec835; webcast_leading_last_show_time=1691016922379; webcast_leading_total_show_times=1; webcast_local_quality=sd; live_can_add_dy_2_desktop=%221%22; msToken=1JDHnVPw_9yTvzIrwb7cQj8dCMNOoesXbA_IooV8cezcOdpe4pzusZE7NB7tZn9TBXPr0ylxmv-KMs5rqbNUBHP4P7VBFUu0ZAht_BEylqrLpzgt3y5ne_38hXDOX8o=; msToken=jV_yeN1IQKUd9PlNtpL7k5vthGKcHo0dEh_QPUQhr8G3cuYv-Jbb4NnIxGDmhVOkZOCSihNpA2kvYtHiTW25XNNX_yrsv5FN8O6zm3qmCIXcEe0LywLn7oBO2gITEeg=; tt_scid=mYfqpfbDjqXrIGJuQ7q-DlQJfUSG51qG.KUdzztuGP83OjuVLXnQHjsz-BRHRJu4e986'
    }
    if cookies:
        headers['Cookie'] = cookies

    async def get_app_data(room_id: str, sec_uid: str) -> dict:
        app_params = {
            "verifyFp": "verify_hwj52020_7szNlAB7_pxNY_48Vh_ALKF_GA1Uf3yteoOY",
            "type_id": "0",
            "live_id": "1",
            "room_id": room_id,
            "sec_user_id": sec_uid,
            "version_code": "99.99.99",
            "app_id": "1128"
        }
        api2 = f'https://webcast.amemv.com/webcast/room/reflow/info/?{urllib.parse.urlencode(app_params)}'
        a_bogus = ab_sign(urllib.parse.urlparse(api2).query, headers['User-Agent'])
        api2 += "&a_bogus=" + a_bogus
        try:
            json_str2 = await async_req(url=api2, proxy_addr=proxy_addr, headers=headers)
            if not json_str2:
                raise Exception("it triggered risk control")
            json_data2 = json.loads(json_str2)['data']
            if not json_data2.get('room'):
                raise Exception(f"{url} VR live is not supported")
            room_data2 = json_data2['room']
            room_data2['anchor_name'] = room_data2['owner']['nickname']
            return room_data2
        except Exception as e:
            raise Exception(f"Douyin app data fetch error, because {e}.")

    try:
        web_rid = url.split('?')[0].split('live.douyin.com/')
        if len(web_rid) > 1:
            return await get_douyin_web_stream_data(url, proxy_addr, cookies)
        else:
            try:
                data = await get_sec_user_id(url, proxy_addr=proxy_addr)
                _room_id, _sec_uid = data
                room_data = await get_app_data(_room_id, _sec_uid)
            except UnsupportedUrlError:
                unique_id = await get_unique_id(url, proxy_addr=proxy_addr)
                return await get_douyin_stream_data(f'https://live.douyin.com/{unique_id}')

        if room_data['status'] == 2:
            if 'stream_url' not in room_data:
                raise RuntimeError(
                    "The live streaming type or gameplay is not supported on the computer side yet, please use the "
                    "app to share the link for recording."
                )
            live_core_sdk_data = room_data['stream_url']['live_core_sdk_data']
            pull_datas = room_data['stream_url']['pull_datas']
            if live_core_sdk_data:
                if pull_datas:
                    key = list(pull_datas.keys())[0]
                    json_str = pull_datas[key]['stream_data']
                else:
                    json_str = live_core_sdk_data['pull_data']['stream_data']
                json_data = json.loads(json_str)
                if 'origin' in json_data['data']:
                    stream_data = live_core_sdk_data['pull_data']['stream_data']
                    origin_data = json.loads(stream_data)['data']['origin']['main']
                    sdk_params = json.loads(origin_data['sdk_params'])
                    origin_hls_codec = sdk_params.get('VCodec') or ''

                    origin_url_list = json_data['data']['origin']['main']
                    origin_m3u8 = {'ORIGIN': origin_url_list["hls"] + '&codec=' + origin_hls_codec}
                    origin_flv = {'ORIGIN': origin_url_list["flv"] + '&codec=' + origin_hls_codec}
                    hls_pull_url_map = room_data['stream_url']['hls_pull_url_map']
                    flv_pull_url = room_data['stream_url']['flv_pull_url']
                    room_data['stream_url']['hls_pull_url_map'] = {**origin_m3u8, **hls_pull_url_map}
                    room_data['stream_url']['flv_pull_url'] = {**origin_flv, **flv_pull_url}
    except Exception as e:
        logger.error(f"抓取失败: {e} (行 {e.__traceback__.tb_lineno})")
        room_data = {'anchor_name': ""}
    return room_data


@trace_error_decorator
async def get_douyin_stream_data(url: str, proxy_addr: OptionalStr = None, cookies: OptionalStr = None) -> dict:
    headers = {
        'User-Agent': 'Mozilla/5.0 (Windows NT 10.0; Win64; x64; rv:109.0) Gecko/20100101 Firefox/115.0',
        'Accept-Language': 'zh-CN,zh;q=0.8,zh-TW;q=0.7,zh-HK;q=0.5,en-US;q=0.3,en;q=0.2',
        'Referer': 'https://live.douyin.com/',
        'Cookie': 'ttwid=1%7CB1qls3GdnZhUov9o2NxOMxxYS2ff6OSvEWbv0ytbES4%7C1680522049%7C280d802d6d478e3e78d0c807f7c487e7ffec0ae4e5fdd6a0fe74c3c6af149511; my_rd=1; passport_csrf_token=3ab34460fa656183fccfb904b16ff742; passport_csrf_token_default=3ab34460fa656183fccfb904b16ff742; d_ticket=9f562383ac0547d0b561904513229d76c9c21; n_mh=hvnJEQ4Q5eiH74-84kTFUyv4VK8xtSrpRZG1AhCeFNI; store-region=cn-fj; store-region-src=uid; LOGIN_STATUS=1; __security_server_data_status=1; FORCE_LOGIN=%7B%22videoConsumedRemainSeconds%22%3A180%7D; pwa2=%223%7C0%7C3%7C0%22; download_guide=%223%2F20230729%2F0%22; volume_info=%7B%22isUserMute%22%3Afalse%2C%22isMute%22%3Afalse%2C%22volume%22%3A0.6%7D; strategyABtestKey=%221690824679.923%22; stream_recommend_feed_params=%22%7B%5C%22cookie_enabled%5C%22%3Atrue%2C%5C%22screen_width%5C%22%3A1536%2C%5C%22screen_height%5C%22%3A864%2C%5C%22browser_online%5C%22%3Atrue%2C%5C%22cpu_core_num%5C%22%3A8%2C%5C%22device_memory%5C%22%3A8%2C%5C%22downlink%5C%22%3A10%2C%5C%22effective_type%5C%22%3A%5C%224g%5C%22%2C%5C%22round_trip_time%5C%22%3A150%7D%22; VIDEO_FILTER_MEMO_SELECT=%7B%22expireTime%22%3A1691443863751%2C%22type%22%3Anull%7D; home_can_add_dy_2_desktop=%221%22; __live_version__=%221.1.1.2169%22; device_web_cpu_core=8; device_web_memory_size=8; xgplayer_user_id=346045893336; csrf_session_id=2e00356b5cd8544d17a0e66484946f28; odin_tt=724eb4dd23bc6ffaed9a1571ac4c757ef597768a70c75fef695b95845b7ffcd8b1524278c2ac31c2587996d058e03414595f0a4e856c53bd0d5e5f56dc6d82e24004dc77773e6b83ced6f80f1bb70627; __ac_nonce=064caded4009deafd8b89; __ac_signature=_02B4Z6wo00f01HLUuwwAAIDBh6tRkVLvBQBy9L-AAHiHf7; ttcid=2e9619ebbb8449eaa3d5a42d8ce88ec835; webcast_leading_last_show_time=1691016922379; webcast_leading_total_show_times=1; webcast_local_quality=sd; live_can_add_dy_2_desktop=%221%22; msToken=1JDHnVPw_9yTvzIrwb7cQj8dCMNOoesXbA_IooV8cezcOdpe4pzusZE7NB7tZn9TBXPr0ylxmv-KMs5rqbNUBHP4P7VBFUu0ZAht_BEylqrLpzgt3y5ne_38hXDOX8o=; msToken=jV_yeN1IQKUd9PlNtpL7k5vthGKcHo0dEh_QPUQhr8G3cuYv-Jbb4NnIxGDmhVOkZOCSihNpA2kvYtHiTW25XNNX_yrsv5FN8O6zm3qmCIXcEe0LywLn7oBO2gITEeg=; tt_scid=mYfqpfbDjqXrIGJuQ7q-DlQJfUSG51qG.KUdzztuGP83OjuVLXnQHjsz-BRHRJu4e986'
    }
    if cookies:
        headers['Cookie'] = cookies

    try:
        origin_url_list = None
        html_str = await async_req(url=url, proxy_addr=proxy_addr, headers=headers)
        match_json_str = re.search(r'(\{\\"state\\":.*?)]\\n"]\)', html_str)
        if not match_json_str:
            match_json_str = re.search(r'(\{\\"common\\":.*?)]\\n"]\)</script><div hidden', html_str)
        json_str = match_json_str.group(1)
        cleaned_string = json_str.replace('\\', '').replace(r'u0026', r'&')
        room_store = re.search('"roomStore":(.*?),"linkmicStore"', cleaned_string, re.DOTALL).group(1)
        anchor_name = re.search('"nickname":"(.*?)","avatar_thumb', room_store, re.DOTALL).group(1)
        room_store = room_store.split(',"has_commerce_goods"')[0] + '}}}'
        json_data = json.loads(room_store)['roomInfo']['room']
        json_data['anchor_name'] = anchor_name
        if 'status' in json_data and json_data['status'] == 4:
            return json_data
        stream_orientation = json_data['stream_url']['stream_orientation']
        match_json_str2 = re.findall(r'"(\{\\"common\\":.*?)"]\)</script><script nonce=', html_str)
        if match_json_str2:
            json_str = match_json_str2[0] if stream_orientation == 1 else match_json_str2[1]
            json_data2 = json.loads(
                json_str.replace('\\', '').replace('"{', '{').replace('}"', '}').replace('u0026', '&'))
            if 'origin' in json_data2['data']:
                origin_url_list = json_data2['data']['origin']['main']

        else:
            html_str = html_str.replace('\\', '').replace('u0026', '&')
            match_json_str3 = re.search('"origin":\\{"main":(.*?),"dash"', html_str, re.DOTALL)
            if match_json_str3:
                origin_url_list = json.loads(match_json_str3.group(1) + '}')

        if origin_url_list:
            origin_hls_codec = origin_url_list['sdk_params'].get('VCodec') or ''
            origin_m3u8 = {'ORIGIN': origin_url_list["hls"] + '&codec=' + origin_hls_codec}
            origin_flv = {'ORIGIN': origin_url_list["flv"] + '&codec=' + origin_hls_codec}
            hls_pull_url_map = json_data['stream_url']['hls_pull_url_map']
            flv_pull_url = json_data['stream_url']['flv_pull_url']
            json_data['stream_url']['hls_pull_url_map'] = {**origin_m3u8, **hls_pull_url_map}
            json_data['stream_url']['flv_pull_url'] = {**origin_flv, **flv_pull_url}
        return json_data

    except Exception as e:
        logger.warning(f"Web 端解析失败 ({url})，切换到 App 端重试: {e}")
        return await get_douyin_app_stream_data(url=url, proxy_addr=proxy_addr, cookies=cookies)
