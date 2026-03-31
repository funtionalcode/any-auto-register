"""
MoeMail API 测试脚本

用于验证 MoeMail (sall.cc) 邮箱生成 API 的完整调用流程
"""

import requests
import random
import string
import time

BASE_URL = "https://sall.cc"

def test_moemail_flow():
    """测试完整的 MoeMail 邮箱生成流程"""

    print("=" * 60)
    print("MoeMail API 测试")
    print("=" * 60)

    # 创建会话
    s = requests.Session()
    s.headers.update({
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/145.0.0.0 Safari/537.36",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/zh-CN/login"
    })

    # ========== 步骤 1: 注册账号 ==========
    print("\n[步骤 1] 注册账号...")
    username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
    print(f"  账号：{username}")
    print(f"  密码：{password}")

    r_reg = s.post(
        f"{BASE_URL}/api/auth/register",
        json={"username": username, "password": password, "turnstileToken": ""},
        timeout=15
    )
    print(f"  状态码：{r_reg.status_code}")
    print(f"  响应：{r_reg.text[:100]}")

    if r_reg.status_code not in [200, 201]:
        print(f"  [!] 注册失败，停止测试")
        return False

    # ========== 步骤 2: 获取 CSRF Token ==========
    print("\n[步骤 2] 获取 CSRF Token...")
    r_csrf = s.get(f"{BASE_URL}/api/auth/csrf", timeout=10)
    print(f"  状态码：{r_csrf.status_code}")

    if r_csrf.status_code != 200:
        print(f"  [!] 获取 CSRF 失败")
        return False

    try:
        csrf = r_csrf.json().get("csrfToken", "")
        print(f"  CSRF Token: {csrf[:20]}..." if len(csrf) > 20 else f"  CSRF Token: {csrf}")
    except Exception as e:
        print(f"  [!] 解析 CSRF 失败：{e}")
        return False

    # ========== 步骤 3: 登录获取 Session ==========
    print("\n[步骤 3] 登录获取 Session...")
    r_login = s.post(
        f"{BASE_URL}/api/auth/callback/credentials",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={BASE_URL}",
        timeout=15,
        allow_redirects=True
    )
    print(f"  状态码：{r_login.status_code}")
    print(f"  Cookies: {[c.name for c in s.cookies]}")

    # 检查 session-token
    session_token = None
    for cookie in s.cookies:
        if "session-token" in cookie.name:
            session_token = cookie.value
            print(f"  Session Token: {session_token[:20]}..." if len(session_token) > 20 else f"  Session Token: {session_token}")
            break

    if not session_token:
        print("  [!] 未获取到 session-token")
        return False

    # ========== 步骤 4: 获取域名配置 ==========
    print("\n[步骤 4] 获取域名配置...")
    r_config = s.get(f"{BASE_URL}/api/config", timeout=10)
    print(f"  状态码：{r_config.status_code}")
    print(f"  响应：{r_config.text[:200]}")

    if r_config.status_code != 200:
        print(f"  [!] 获取配置失败")
        return False

    try:
        domain_str = r_config.json().get("emailDomains", "sall.cc")
        print(f"  域名配置字符串：{domain_str}")
        domains = [d.strip() for d in str(domain_str).split(",") if d.strip()]
        print(f"  解析后的域名列表：{domains}")
        domain = random.choice(domains) if domains else "sall.cc"
        print(f"  选择的域名：{domain}")
    except Exception as e:
        print(f"  [!] 解析域名失败：{e}")
        domain = "sall.cc"

    # ========== 步骤 5: 生成邮箱地址 ==========
    print("\n[步骤 5] 生成邮箱地址...")

    # 使用 nanoid 字符集
    chars = string.ascii_letters + string.digits + "_-"
    name = "".join(random.choices(chars, k=8))
    print(f"  随机名：{name}")

    r_gen = s.post(
        f"{BASE_URL}/api/emails/generate",
        json={"name": name, "domain": domain, "expiryTime": 86400000},
        timeout=15
    )
    print(f"  状态码：{r_gen.status_code}")
    print(f"  响应：{r_gen.text}")

    if r_gen.status_code == 401:
        print("  [!] 401 未授权：Session 可能已过期或未正确传递 Cookie")
        return False
    elif r_gen.status_code == 409:
        print("  [!] 409 冲突：邮箱已被使用")
        return False
    elif r_gen.status_code == 400:
        print(f"  [!] 400 错误：{r_gen.text}")
        return False
    elif r_gen.status_code == 200:
        try:
            data = r_gen.json()
            email_id = data.get("id")
            email_addr = data.get("address")
            print(f"  [✓] 邮箱生成成功!")
            print(f"      邮箱地址：{email_addr}")
            print(f"      邮箱 ID: {email_id}")
            return True
        except Exception as e:
            print(f"  [!] 解析响应失败：{e}")
            return False
    else:
        print(f"  [!] 未知错误：状态码 {r_gen.status_code}")
        return False


def test_moemail_with_existing_session():
    """测试使用固定账号登录生成邮箱"""
    import os

    # 从环境变量或配置文件读取已有账号
    test_username = os.environ.get("MOEMAIL_TEST_USERNAME")
    test_password = os.environ.get("MOEMAIL_TEST_PASSWORD")

    if not test_username or not test_password:
        print("\n[!] 未配置测试账号，跳过此测试")
        print("    设置环境变量: MOEMAIL_TEST_USERNAME 和 MOEMAIL_TEST_PASSWORD")
        return False

    print("=" * 60)
    print("MoeMail API 测试 (使用已有账号)")
    print("=" * 60)

    # 创建会话
    s = requests.Session()
    s.headers.update({
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/zh-CN/login"
    })

    # 获取 CSRF
    print("\n[步骤 1] 获取 CSRF Token...")
    r_csrf = s.get(f"{BASE_URL}/api/auth/csrf", timeout=10)
    csrf = r_csrf.json().get("csrfToken", "")
    print(f"  CSRF Token: {csrf[:20]}...")

    # 登录
    print("\n[步骤 2] 登录...")
    r_login = s.post(
        f"{BASE_URL}/api/auth/callback/credentials",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=f"username={test_username}&password={test_password}&csrfToken={csrf}&redirect=false&callbackUrl={BASE_URL}",
        timeout=15,
        allow_redirects=True
    )
    print(f"  状态码：{r_login.status_code}")

    # 检查 session-token
    session_token = None
    for cookie in s.cookies:
        if "session-token" in cookie.name:
            session_token = cookie.value
            break

    if not session_token:
        print("  [!] 登录失败，未获取到 session-token")
        return False
    print("  [✓] 登录成功")

    # 获取配置
    print("\n[步骤 3] 获取域名配置...")
    r_config = s.get(f"{BASE_URL}/api/config", timeout=10)
    domain_str = r_config.json().get("emailDomains", "sall.cc")
    domains = [d.strip() for d in str(domain_str).split(",") if d.strip()]
    domain = random.choice(domains) if domains else "sall.cc"
    print(f"  域名：{domain}")

    # 生成邮箱
    print("\n[步骤 4] 生成邮箱...")
    chars = string.ascii_letters + string.digits + "_-"
    name = "".join(random.choices(chars, k=8))

    r_gen = s.post(
        f"{BASE_URL}/api/emails/generate",
        json={"name": name, "domain": domain, "expiryTime": 86400000},
        timeout=15
    )
    print(f"  状态码：{r_gen.status_code}")
    print(f"  响应：{r_gen.text}")

    if r_gen.status_code == 200:
        data = r_gen.json()
        print(f"  [✓] 邮箱生成成功：{data.get('address')}")
        return True
    else:
        print(f"  [!] 生成失败")
        return False


def test_moemail_receive_email():
    """测试接收邮件"""
    print("\n" + "=" * 60)
    print("MoeMail API 测试 - 接收邮件")
    print("=" * 60)

    # 先创建一个新账号并生成邮箱
    s = requests.Session()
    s.headers.update({
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/zh-CN/login"
    })

    # 注册
    username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"

    s.post(
        f"{BASE_URL}/api/auth/register",
        json={"username": username, "password": password, "turnstileToken": ""},
        timeout=15
    )

    # 获取 CSRF
    r_csrf = s.get(f"{BASE_URL}/api/auth/csrf", timeout=10)
    csrf = r_csrf.json().get("csrfToken", "")

    # 登录
    s.post(
        f"{BASE_URL}/api/auth/callback/credentials",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={BASE_URL}",
        timeout=15,
        allow_redirects=True
    )

    # 生成邮箱
    r_config = s.get(f"{BASE_URL}/api/config", timeout=10)
    domain_str = r_config.json().get("emailDomains", "sall.cc")
    domains = [d.strip() for d in str(domain_str).split(",") if d.strip()]
    domain = random.choice(domains) if domains else "sall.cc"

    chars = string.ascii_letters + string.digits + "_-"
    name = "".join(random.choices(chars, k=8))

    r_gen = s.post(
        f"{BASE_URL}/api/emails/generate",
        json={"name": name, "domain": domain, "expiryTime": 86400000},
        timeout=15
    )

    if r_gen.status_code != 200:
        print(f"  [!] 邮箱生成失败")
        return False

    data = r_gen.json()
    email_id = data.get("id")
    email_addr = data.get("address")
    print(f"  邮箱：{email_addr} (ID: {email_id})")

    # 测试获取收件箱
    print("\n[测试] 获取收件箱...")
    r_inbox = s.get(f"{BASE_URL}/api/emails/{email_id}", timeout=10)
    print(f"  状态码：{r_inbox.status_code}")
    print(f"  响应：{r_inbox.text[:200]}")

    if r_inbox.status_code == 200:
        inbox_data = r_inbox.json()
        messages = inbox_data.get("messages", [])
        print(f"  邮件数量：{len(messages)}")
        return True
    else:
        print(f"  [!] 获取收件箱失败")
        return False


if __name__ == "__main__":
    import sys

    test_type = sys.argv[1] if len(sys.argv) > 1 else "basic"

    if test_type == "basic":
        success = test_moemail_flow()
    elif test_type == "existing":
        success = test_moemail_with_existing_session()
    elif test_type == "receive":
        success = test_moemail_receive_email()
    else:
        print("用法：python moemail_api_test.py [basic|existing|receive]")
        success = False

    print("\n" + "=" * 60)
    if success:
        print("测试结果：[✓] 通过")
    else:
        print("测试结果：[✗] 失败")
    print("=" * 60)

    sys.exit(0 if success else 1)
