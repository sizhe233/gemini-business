"""
邮箱服务提供者抽象层

支持多种邮箱服务：
1. CloudflareMailProvider - Cloudflare Worker 临时邮箱服务
2. ChatGPTMailProvider - mail.chatgpt.org.uk 临时邮箱服务

通过 MAIL_PROVIDER 环境变量选择使用哪种服务
"""
import json
import time
import logging
from abc import ABC, abstractmethod
from typing import Optional

import requests
from bs4 import BeautifulSoup

logger = logging.getLogger("gemini.mail_providers")


class MailProvider(ABC):
    """邮箱服务抽象基类"""

    @abstractmethod
    def create_email(self, domain: Optional[str] = None) -> Optional[str]:
        """
        创建临时邮箱

        Args:
            domain: 指定域名（部分服务支持）

        Returns:
            邮箱地址，失败返回 None
        """
        pass

    @abstractmethod
    def get_verification_code(self, email: str, sender: str, timeout: int = 60) -> Optional[str]:
        """
        获取验证码

        Args:
            email: 邮箱地址
            sender: 发件人邮箱（用于过滤）
            timeout: 超时时间（秒）

        Returns:
            验证码字符串，失败返回 None
        """
        pass

    @abstractmethod
    def supports_refresh(self) -> bool:
        """
        是否支持刷新 token（邮箱是否持久化）

        如果邮箱是临时的（用完即弃），返回 False
        如果邮箱是持久化的（可以持续接收邮件），返回 True
        """
        pass

    @property
    @abstractmethod
    def name(self) -> str:
        """服务名称"""
        pass


class CloudflareMailProvider(MailProvider):
    """
    Cloudflare Worker 临时邮箱服务

    API:
    - POST /admin/new_address - 创建邮箱
    - GET /admin/mails - 获取邮件列表
    """

    def __init__(self, api_url: str, admin_key: str, email_domains: list, supports_refresh: bool = True):
        """
        初始化 Cloudflare 邮箱服务

        Args:
            api_url: API 基础 URL
            admin_key: 管理员密钥
            email_domains: 可用域名列表
            supports_refresh: 是否支持刷新（默认 True，假设是持久化服务）
        """
        self.api_url = api_url.rstrip('/')
        self.admin_key = admin_key
        self.email_domains = email_domains if email_domains else []
        self._supports_refresh = supports_refresh

    @property
    def name(self) -> str:
        return "cloudflare"

    def supports_refresh(self) -> bool:
        return self._supports_refresh

    def create_email(self, domain: Optional[str] = None) -> Optional[str]:
        """创建临时邮箱"""
        if not self.api_url or not self.admin_key:
            logger.error("❌ Cloudflare 邮箱 API 未配置")
            return None

        if not self.email_domains:
            logger.error("❌ Cloudflare 邮箱域名未配置")
            return None

        try:
            import random
            from string import ascii_letters, digits

            # 生成随机用户名
            random_name = ''.join(random.sample(ascii_letters + digits, 10))

            # 如果未指定域名，从列表中随机选择
            if not domain:
                domain = random.choice(self.email_domains)

            json_data = {
                "enablePrefix": False,
                "name": random_name,
                "domain": domain
            }

            r = requests.post(
                f"{self.api_url}/admin/new_address",
                headers={"x-admin-auth": self.admin_key},
                json=json_data,
                timeout=30,
                verify=False
            )

            if r.status_code == 200:
                email = r.json().get('address')
                logger.info(f"✅ Cloudflare 邮箱创建成功: {email}")
                return email
            else:
                logger.error(f"❌ Cloudflare 邮箱创建失败: {r.status_code} - {r.text}")

        except Exception as e:
            logger.error(f"❌ Cloudflare 邮箱创建异常: {e}")

        return None

    def get_verification_code(self, email: str, sender: str, timeout: int = 60) -> Optional[str]:
        """获取验证码"""
        logger.info(f"⏳ [Cloudflare] 等待验证码 [{email}]...")
        start = time.time()

        while time.time() - start < timeout:
            try:
                r = requests.get(
                    f"{self.api_url}/admin/mails?limit=20&offset=0",
                    headers={"x-admin-auth": self.admin_key},
                    timeout=10,
                    verify=False
                )

                if r.status_code == 200:
                    emails = r.json().get('results', {})
                    for mail in emails:
                        if mail.get("address") == email and mail.get("source") == sender:
                            metadata = json.loads(mail["metadata"])
                            code = metadata["ai_extract"]["result"]
                            logger.info(f"✅ [Cloudflare] 验证码获取成功: {code}")
                            return code

            except Exception as e:
                logger.debug(f"[Cloudflare] 获取邮件异常: {e}")

            time.sleep(2)

        logger.error(f"❌ [Cloudflare] 验证码超时 [{email}]")
        return None


class ChatGPTMailProvider(MailProvider):

    def __init__(self, api_url: str = "", api_key: str = "", supports_refresh: bool = True):
        """
        初始化 ChatGPT Mail 服务

        Args:
            api_url: API 基础 URL
            api_key: API 密钥
            supports_refresh: 是否支持刷新（默认 True）
        """
        self.api_url = api_url.rstrip('/')
        self.api_key = api_key
        self._supports_refresh = supports_refresh

    @property
    def name(self) -> str:
        return "chatgpt"

    def supports_refresh(self) -> bool:
        return self._supports_refresh

    def create_email(self, domain: Optional[str] = None) -> Optional[str]:
        """创建临时邮箱（domain 参数在此服务中被忽略）"""
        try:
            r = requests.get(
                f"{self.api_url}/api/generate-email",
                headers={"X-API-Key": self.api_key},
                timeout=30
            )

            if r.status_code == 200 and r.json().get('success'):
                email = r.json()['data']['email']
                logger.info(f"✅ [ChatGPT Mail] 邮箱创建成功: {email}")
                return email
            else:
                logger.error(f"❌ [ChatGPT Mail] 邮箱创建失败: {r.status_code} - {r.text}")

        except Exception as e:
            logger.error(f"❌ [ChatGPT Mail] 邮箱创建异常: {e}")

        return None

    def get_verification_code(self, email: str, sender: str, timeout: int = 60) -> Optional[str]:
        logger.info(f"⏳ [ChatGPT Mail] 等待验证码 [{email}]...")
        start = time.time()

        while time.time() - start < timeout:
            try:
                r = requests.get(
                    f"{self.api_url}/api/emails",
                    params={"email": email},
                    headers={"X-API-Key": self.api_key},
                    timeout=10
                )

                if r.status_code == 200:
                    response_data = r.json()
                    emails = response_data.get('data', {}).get('emails', [])
                    
                    if emails:
                        latest_email = emails[0]
                        html = latest_email.get('html_content') or latest_email.get('content', '')
                        
                        if html:
                            soup = BeautifulSoup(html, 'html.parser')
                            span = soup.find('span', class_='verification-code')
                            if span:
                                code = span.get_text().strip()
                                if len(code) == 6:
                                    logger.info(f"✅ [ChatGPT Mail] 验证码获取成功: {code}")
                                    return code
                            
                            import re
                            code_match = re.search(r'\b(\d{6})\b', html)
                            if code_match:
                                code = code_match.group(1)
                                logger.info(f"✅ [ChatGPT Mail] 验证码(正则匹配)获取成功: {code}")
                                return code
                        else:
                            logger.debug(f"[ChatGPT Mail] 邮件内容为空: {latest_email.get('subject', 'no subject')}")
                    else:
                        logger.debug(f"[ChatGPT Mail] 暂无邮件")
                else:
                    logger.debug(f"[ChatGPT Mail] API 响应错误: {r.status_code}")

            except Exception as e:
                logger.debug(f"[ChatGPT Mail] 获取邮件异常: {e}")

            print(f"  等待验证码... ({int(time.time() - start)}s)", end='\r')
            time.sleep(2)

        logger.error(f"❌ [ChatGPT Mail] 验证码超时 [{email}]")
        return None


# ==================== 工厂函数 ====================

def get_mail_provider(
    provider_type: str,
    cloudflare_api_url: str = "",
    cloudflare_admin_key: str = "",
    cloudflare_email_domains: Optional[list] = None,
    chatgpt_api_url: str = "",
    chatgpt_api_key: str = "",
    supports_refresh: bool = True
) -> Optional[MailProvider]:
    """
    获取邮箱服务提供者

    Args:
        provider_type: 服务类型 ("cloudflare" 或 "chatgpt")
        其他参数: 各服务的配置

    Returns:
        MailProvider 实例，配置无效返回 None
    """
    provider_type = provider_type.lower().strip()

    if provider_type == "cloudflare":
        if not cloudflare_api_url or not cloudflare_admin_key:
            logger.error("❌ Cloudflare 邮箱服务配置不完整")
            return None
        return CloudflareMailProvider(
            api_url=cloudflare_api_url,
            admin_key=cloudflare_admin_key,
            email_domains=cloudflare_email_domains or [],
            supports_refresh=supports_refresh
        )

    elif provider_type == "chatgpt":
        if not chatgpt_api_url or not chatgpt_api_key:
            logger.error("❌ ChatGPT Mail 邮箱服务配置不完整")
            return None
        return ChatGPTMailProvider(
            api_url=chatgpt_api_url,
            api_key=chatgpt_api_key,
            supports_refresh=supports_refresh
        )

    else:
        logger.error(f"❌ 未知的邮箱服务类型: {provider_type}")
        return None


def create_mail_provider_from_config() -> Optional[MailProvider]:
    """
    从配置创建邮箱服务提供者

    读取 core.config 中的配置，自动选择并初始化对应的 Provider
    """
    from core.config import config

    provider_type = config.basic.mail_provider
    supports_refresh = config.basic.mail_provider_supports_refresh

    if provider_type == "cloudflare":
        return get_mail_provider(
            provider_type="cloudflare",
            cloudflare_api_url=config.basic.mail_api,
            cloudflare_admin_key=config.basic.mail_admin_key,
            cloudflare_email_domains=config.basic.email_domain,
            supports_refresh=supports_refresh
        )

    elif provider_type == "chatgpt":
        return get_mail_provider(
            provider_type="chatgpt",
            chatgpt_api_url=config.basic.chatgpt_mail_api,
            chatgpt_api_key=config.basic.chatgpt_mail_key,
            supports_refresh=supports_refresh
        )

    else:
        logger.warning(f"⚠️ 未配置邮箱服务类型，默认使用 cloudflare")
        return get_mail_provider(
            provider_type="cloudflare",
            cloudflare_api_url=config.basic.mail_api,
            cloudflare_admin_key=config.basic.mail_admin_key,
            cloudflare_email_domains=config.basic.email_domain,
            supports_refresh=supports_refresh
        )
