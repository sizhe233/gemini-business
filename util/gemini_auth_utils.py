"""
Gemini Business 认证工具类
抽取注册和登录服务的公共逻辑，遵循 DRY 原则
"""
import json
import time
import logging
from typing import Optional, Dict, Any, List
from urllib.parse import urlparse, parse_qs
from datetime import datetime

import requests
from core.config import config
from util.mail_providers import create_mail_provider_from_config, MailProvider

logger = logging.getLogger("gemini.auth_utils")


class GeminiAuthConfig:
    def __init__(self):
        self.mail_api = config.basic.mail_api
        self.admin_key = config.basic.mail_admin_key
        self.email_domains = config.basic.email_domain
        self.google_mail = config.basic.google_mail
        self.login_url = config.security.login_url
        self.mail_provider = config.basic.mail_provider
        self.mail_provider_supports_refresh = config.basic.mail_provider_supports_refresh
        self.chatgpt_mail_api = config.basic.chatgpt_mail_api
        self.chatgpt_mail_key = config.basic.chatgpt_mail_key

    def validate(self) -> bool:
        required = [self.google_mail, self.login_url]
        if self.mail_provider == "cloudflare":
            required.extend([self.mail_api, self.admin_key])
        return all(required)


class GeminiAuthHelper:
    XPATH = {
        "email_input": "/html/body/c-wiz/div/div/div[1]/div/div/div/form/div[1]/div[1]/div/span[2]/input",
        "continue_btn": "/html/body/c-wiz/div/div/div[1]/div/div/div/form/div[2]/div/button",
        "verify_btn": "/html/body/c-wiz/div/div/div[1]/div/div/div/form/div[2]/div/div[1]/span/div[1]/button",
    }

    def __init__(self, config: GeminiAuthConfig):
        self.config = config
        self._mail_provider: Optional[MailProvider] = None

    @property
    def mail_provider(self) -> Optional[MailProvider]:
        if self._mail_provider is None:
            self._mail_provider = create_mail_provider_from_config()
        return self._mail_provider

    def get_verification_code(self, email: str, timeout: int = 60) -> Optional[str]:
        if self.mail_provider is None:
            logger.error("❌ 邮箱服务未配置")
            return None
        return self.mail_provider.get_verification_code(email, self.config.google_mail, timeout)

    def perform_email_verification(self, driver, wait, email: str) -> Dict[str, Any]:
        """
        执行邮箱验证流程（公共方法）
        从输入邮箱到验证码验证完成

        返回: {"success": bool, "error": str|None}
        """
        try:
            from selenium.webdriver.common.by import By
            from selenium.webdriver.support import expected_conditions as EC

            # 1. 输入邮箱
            inp = wait.until(EC.element_to_be_clickable((By.XPATH, self.XPATH["email_input"])))
            inp.click()
            inp.clear()
            for c in email:
                inp.send_keys(c)
                time.sleep(0.02)

            # 2. 点击继续
            time.sleep(0.5)
            btn = wait.until(EC.element_to_be_clickable((By.XPATH, self.XPATH["continue_btn"])))
            driver.execute_script("arguments[0].click();", btn)
            time.sleep(2)

            # 3. 获取验证码
            code = self.get_verification_code(email)
            if not code:
                return {"success": False, "error": "验证码超时"}

            # 4. 输入验证码
            time.sleep(1)
            try:
                pin = wait.until(EC.presence_of_element_located((By.CSS_SELECTOR, "input[name='pinInput']")))
                pin.click()
                time.sleep(0.1)
                for c in code:
                    pin.send_keys(c)
                    time.sleep(0.05)
            except:
                try:
                    span = driver.find_element(By.CSS_SELECTOR, "span[data-index='0']")
                    span.click()
                    time.sleep(0.2)
                    driver.switch_to.active_element.send_keys(code)
                except Exception as e:
                    return {"success": False, "error": f"验证码输入失败: {e}"}

            # 5. 点击验证按钮
            time.sleep(0.5)
            try:
                vbtn = driver.find_element(By.XPATH, self.XPATH["verify_btn"])
                driver.execute_script("arguments[0].click();", vbtn)
            except:
                for btn in driver.find_elements(By.TAG_NAME, "button"):
                    if '验证' in btn.text:
                        driver.execute_script("arguments[0].click();", btn)
                        break

            return {"success": True, "error": None}

        except Exception as e:
            return {"success": False, "error": str(e)}

    def extract_config_from_workspace(self, driver) -> Dict[str, Any]:
        """
        从工作台页面提取配置信息（公共方法）

        返回: {"success": bool, "config": dict|None, "error": str|None}
        """
        try:
            time.sleep(3)  # 等待页面完全加载
            cookies = driver.get_cookies()
            url = driver.current_url
            parsed = urlparse(url)

            # 解析 config_id
            path_parts = url.split('/')
            config_id = None
            for i, p in enumerate(path_parts):
                if p == 'cid' and i + 1 < len(path_parts):
                    config_id = path_parts[i + 1].split('?')[0]
                    break

            cookie_dict = {c['name']: c for c in cookies}
            ses_cookie = cookie_dict.get('__Secure-C_SES', {})
            host_cookie = cookie_dict.get('__Host-C_OSES', {})
            csesidx = parse_qs(parsed.query).get('csesidx', [None])[0]

            if not all([ses_cookie.get('value'), host_cookie.get('value'), csesidx, config_id]):
                return {"success": False, "config": None, "error": "配置数据不完整"}

            config_data = {
                "csesidx": csesidx,
                "config_id": config_id,
                "secure_c_ses": ses_cookie.get('value'),
                "host_c_oses": host_cookie.get('value'),
                "expires_at": datetime.fromtimestamp(
                    ses_cookie.get('expiry', 0) - 43200
                ).strftime('%Y-%m-%d %H:%M:%S') if ses_cookie.get('expiry') else None
            }

            return {"success": True, "config": config_data, "error": None}

        except Exception as e:
            return {"success": False, "config": None, "error": str(e)}

    def wait_for_workspace(self, driver, timeout: int = 30, max_crash_retries: int = 3) -> bool:
        """
        等待进入工作台（公共方法，带崩溃重试）

        Args:
            driver: Selenium WebDriver 实例
            timeout: 等待超时时间（秒）
            max_crash_retries: 崩溃后最大重试次数
            
        返回: True 表示成功进入，False 表示超时或失败
        """
        crash_count = 0
        workspace_url = "https://business.gemini.google/"
        
        for _ in range(timeout):
            time.sleep(1)
            try:
                # 检查页面是否崩溃
                page_source = driver.page_source
                is_crashed = 'crashed' in page_source.lower() or 'aw, snap' in page_source.lower()
                
                if is_crashed:
                    crash_count += 1
                    logger.warning(f"⚠️ 等待工作台时页面崩溃，尝试开新标签页 (崩溃 {crash_count}/{max_crash_retries})")
                    if crash_count >= max_crash_retries:
                        logger.error("❌ 页面崩溃次数过多，放弃重试")
                        return False
                    
                    # 开新标签页并切换
                    if self._recover_from_crash(driver, workspace_url):
                        time.sleep(3)
                        continue
                    else:
                        return False
                
                url = driver.current_url
                if 'business.gemini.google' in url and '/cid/' in url:
                    return True
                    
            except Exception as e:
                error_msg = str(e).lower()
                if 'crash' in error_msg or 'tab' in error_msg or 'target window' in error_msg:
                    crash_count += 1
                    logger.warning(f"⚠️ 等待工作台时检测到崩溃: {e} (崩溃 {crash_count}/{max_crash_retries})")
                    if crash_count >= max_crash_retries:
                        logger.error("❌ 页面崩溃次数过多，放弃重试")
                        return False
                    
                    if self._recover_from_crash(driver, workspace_url):
                        time.sleep(3)
                        continue
                    else:
                        return False
                # 其他异常继续等待
                
        return False
    
    def _recover_from_crash(self, driver, target_url: str) -> bool:
        """
        从崩溃中恢复：开新标签页访问目标URL
        
        艹，崩溃的标签页刷新没用，得开新的！
        """
        try:
            # 获取当前所有窗口句柄
            original_handles = driver.window_handles
            
            # 开新标签页
            driver.execute_script("window.open('');")
            time.sleep(0.5)
            
            # 获取新窗口句柄
            new_handles = driver.window_handles
            new_handle = None
            for handle in new_handles:
                if handle not in original_handles:
                    new_handle = handle
                    break
            
            if not new_handle:
                logger.error("❌ 无法创建新标签页")
                return False
            
            # 切换到新标签页
            driver.switch_to.window(new_handle)
            
            # 关闭旧的崩溃标签页
            for handle in original_handles:
                try:
                    driver.switch_to.window(handle)
                    driver.close()
                except:
                    pass
            
            # 切回新标签页
            driver.switch_to.window(new_handle)
            
            # 访问目标URL
            driver.get(target_url)
            time.sleep(3)
            
            logger.info("✅ 已通过新标签页恢复")
            return True
            
        except Exception as e:
            logger.error(f"❌ 恢复失败: {e}")
            return False

    def extract_config_with_retry(self, driver, max_retries: int = 3) -> Dict[str, Any]:
        """
        带重试机制的配置提取（处理 tab crashed 问题）
        
        艹，Google 工作台页面经常崩溃，这个方法会自动重试
        
        Args:
            driver: Selenium WebDriver 实例
            max_retries: 最大重试次数，默认3次
            
        返回: {"success": bool, "config": dict|None, "error": str|None}
        """
        extract_result = None
        last_error = None
        
        for attempt in range(max_retries):
            try:
                # 检查页面是否崩溃
                page_source = driver.page_source
                if 'crashed' in page_source.lower() or 'aw, snap' in page_source.lower():
                    logger.warning(f"⚠️ 页面崩溃，尝试刷新 (尝试 {attempt + 1}/{max_retries})")
                    driver.refresh()
                    time.sleep(3)
                    continue
                
                extract_result = self.extract_config_from_workspace(driver)
                if extract_result["success"]:
                    return extract_result
                else:
                    last_error = extract_result["error"]
                    logger.warning(f"⚠️ 提取配置失败: {last_error}，尝试刷新 (尝试 {attempt + 1}/{max_retries})")
                    driver.refresh()
                    time.sleep(3)
                    
            except Exception as e:
                error_msg = str(e).lower()
                if 'crash' in error_msg or 'tab' in error_msg:
                    logger.warning(f"⚠️ 检测到页面崩溃: {e}，尝试刷新 (尝试 {attempt + 1}/{max_retries})")
                    try:
                        driver.refresh()
                        time.sleep(3)
                    except:
                        # 如果刷新也失败，尝试重新访问工作台
                        try:
                            driver.get("https://business.gemini.google/")
                            time.sleep(5)
                        except:
                            pass
                else:
                    last_error = str(e)
                    logger.warning(f"⚠️ 提取配置异常: {e}，尝试刷新 (尝试 {attempt + 1}/{max_retries})")
                    try:
                        driver.refresh()
                        time.sleep(3)
                    except:
                        pass
        
        return {"success": False, "config": None, "error": last_error or "提取配置失败（已重试）"}

