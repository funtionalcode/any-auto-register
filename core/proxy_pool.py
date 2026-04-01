"""代理池 - 从数据库读取代理，支持轮询和按区域选取"""

from typing import Optional
from sqlmodel import Session, select
from .db import ProxyModel, engine
from .proxy_utils import build_requests_proxy_config, normalize_proxy_url
import time, threading, random
from datetime import datetime, timezone


class ProxyPool:
    def __init__(self):
        self._index = 0
        self._lock = threading.Lock()

    def get_next(self, region: str = "") -> Optional[str]:
        """加权轮询取一个可用代理，在高成功率代理间轮换"""
        with Session(engine) as s:
            q = select(ProxyModel).where(ProxyModel.is_active == True)
            if region:
                q = q.where(ProxyModel.region == region)
            proxies = s.exec(q).all()
            if not proxies:
                return None
            proxies.sort(
                key=lambda p: p.success_count / max(p.success_count + p.fail_count, 1),
                reverse=True,
            )
            with self._lock:
                idx = self._index % len(proxies)
                self._index += 1
            return proxies[idx].url

    def report_success(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.success_count += 1
                p.last_checked = datetime.now(timezone.utc)
                s.add(p)
                s.commit()

    def report_fail(self, url: str) -> None:
        with Session(engine) as s:
            p = s.exec(select(ProxyModel).where(ProxyModel.url == url)).first()
            if p:
                p.fail_count += 1
                p.last_checked = datetime.now(timezone.utc)
                # 连续失败超过10次自动禁用
                if p.fail_count > 0 and p.success_count == 0 and p.fail_count >= 5:
                    p.is_active = False
                s.add(p)
                s.commit()

    @staticmethod
    def _extract_ip_from_response(data: dict) -> str:
        origin = str((data or {}).get("origin") or (data or {}).get("ip") or "").strip()
        if not origin:
            return ""
        return origin.split(",")[0].strip()

    @staticmethod
    def _lookup_ip_metadata(ip: str) -> dict:
        import requests

        if not ip:
            return {}

        try:
            response = requests.get(
                f"https://ipwho.is/{ip}",
                timeout=8,
            )
            data = response.json() if response.text else {}
            if isinstance(data, dict) and data.get("success", True) is not False:
                country_code = str(data.get("country_code") or "").strip()
                country = str(data.get("country") or "").strip()
                region_name = str(data.get("region") or "").strip()
                city = str(data.get("city") or "").strip()
                parts = []
                if country_code:
                    parts.append(country_code)
                elif country:
                    parts.append(country)
                if region_name and region_name not in parts:
                    parts.append(region_name)
                region_label = " / ".join([part for part in parts if part])
                return {
                    "country_code": country_code,
                    "country": country,
                    "region_name": region_name,
                    "city": city,
                    "region_label": region_label or country or country_code,
                }
        except Exception:
            pass
        return {}

    def test_proxy(self, url: str, *, update_stats: bool = False) -> dict:
        """测试单个代理，返回连通性、出口 IP 和地区信息。"""
        import requests

        raw_url = str(url or "").strip()
        if not raw_url:
            raise ValueError("代理地址不能为空")

        normalized_url = normalize_proxy_url(raw_url) or raw_url
        started_at = time.perf_counter()
        try:
            response = requests.get(
                "https://httpbin.org/ip",
                proxies=build_requests_proxy_config(normalized_url),
                timeout=10,
            )
            response.raise_for_status()
            ip = self._extract_ip_from_response(response.json())
            geo = self._lookup_ip_metadata(ip)
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            if update_stats:
                self.report_success(raw_url)
            return {
                "ok": True,
                "url": raw_url,
                "normalized_url": normalized_url,
                "ip": ip,
                "latency_ms": latency_ms,
                **geo,
            }
        except Exception as e:
            latency_ms = int((time.perf_counter() - started_at) * 1000)
            if update_stats:
                self.report_fail(raw_url)
            return {
                "ok": False,
                "url": raw_url,
                "normalized_url": normalized_url,
                "latency_ms": latency_ms,
                "error": str(e),
            }

    def check_all(self) -> dict:
        """检测所有代理可用性"""
        with Session(engine) as s:
            proxies = s.exec(select(ProxyModel)).all()
        results = {"ok": 0, "fail": 0}
        for p in proxies:
            result = self.test_proxy(p.url, update_stats=True)
            if result.get("ok"):
                results["ok"] += 1
            else:
                results["fail"] += 1
        return results


proxy_pool = ProxyPool()
