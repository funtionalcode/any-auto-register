# MoeMail API 测试文档

## API 基础信息

- **Base URL**: `https://sall.cc` (可配置)
- **认证方式**: Cookie Session Token
- **运行环境**: Edge Runtime

## 认证流程

### 1. 注册账号

**请求**:
```http
POST /api/auth/register
Content-Type: application/json

{
  "username": "testuser123",
  "password": "Test12345678!",
  "turnstileToken": ""
}
```

**成功响应 (200)**:
```json
{
  "status": "success"
}
```

**错误响应**:
- 400: 用户名或密码格式错误
- 409: 用户名已存在

### 2. 获取 CSRF Token

**请求**:
```http
GET /api/auth/csrf
```

**成功响应 (200)**:
```json
{
  "csrfToken": "abc123def456..."
}
```

### 3. 登录获取 Session

**请求**:
```http
POST /api/auth/callback/credentials
Content-Type: application/x-www-form-urlencoded

username=testuser123&password=Test12345678!&csrfToken=abc123def456&redirect=false&callbackUrl=https://sall.cc
```

**成功响应**:
- 返回 302 重定向
- Set-Cookie: `session-token=xxx; Path=/; HttpOnly`

**关键点**: 登录后 Cookie 中会包含 `session-token`，后续请求需要携带此 Cookie

## 邮箱生成 API

### 1. 获取域名配置

**请求**:
```http
GET /api/config
Cookie: session-token=xxx
```

**成功响应 (200)**:
```json
{
  "emailDomains": "sall.cc,moemail.app,tempmail.org"
}
```

**注意**: 返回的是逗号分隔的字符串，不是数组

### 2. 生成邮箱地址

**请求**:
```http
POST /api/emails/generate
Content-Type: application/json
Cookie: session-token=xxx

{
  "name": "random8ch",
  "domain": "sall.cc",
  "expiryTime": 86400000
}
```

**参数说明**:
| 参数 | 类型 | 必填 | 说明 |
|------|------|------|------|
| name | string | 否 | 邮箱前缀，不填则服务端生成随机 8 位 |
| domain | string | 是 | 邮箱域名，必须是配置中的域名 |
| expiryTime | number | 是 | 过期时间（毫秒），0 表示永久 |

**成功响应 (200)**:
```json
{
  "id": "uuid-123-456",
  "address": "random8ch@sall.cc"
}
```

**错误响应**:
- 400: 无效域名
- 401: 未登录/Session 过期
- 409: 邮箱地址已被使用
- 429: 超出邮箱数量限制

## 收件箱 API

### 获取邮件列表

**请求**:
```http
GET /api/emails/{email_id}
Cookie: session-token=xxx
```

**成功响应 (200)**:
```json
{
  "messages": [
    {
      "id": "msg-uuid",
      "from": "sender@example.com",
      "subject": "验证码",
      "content": "您的验证码是 123456",
      "receivedAt": "2026-03-31T11:00:00Z"
    }
  ]
}
```

## 常见问题排查

### 401 Unauthorized

**原因**:
1. 未完成注册/登录流程
2. Session token 过期
3. Cookie 未正确传递

**解决方法**:
- 每次生成新邮箱前重新执行完整的注册 + 登录流程
- 确保请求时携带了包含 session-token 的 Cookie

### 409 Conflict

**原因**: 邮箱地址已被使用

**解决方法**:
- 使用新的随机名重试

### 400 Bad Request (Invalid Domain)

**原因**: 使用了未配置的域名

**解决方法**:
- 先调用 `/api/config` 获取可用域名列表

## 完整调用流程

```
1. POST /api/auth/register → 注册账号
2. GET /api/auth/csrf → 获取 CSRF Token
3. POST /api/auth/callback/credentials → 登录，获取 session-token Cookie
4. GET /api/config → 获取可用域名列表
5. POST /api/emails/generate → 生成邮箱
   - 如返回 409，更换随机名重试
6. GET /api/emails/{email_id} → 轮询收件箱
```

## Python 测试脚本

```python
import requests
import random
import string

BASE_URL = "https://sall.cc"

def test_moemail_flow():
    # 创建会话
    s = requests.Session()
    s.headers.update({
        "user-agent": "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) AppleWebKit/537.36",
        "origin": BASE_URL,
        "referer": f"{BASE_URL}/zh-CN/login"
    })
    
    # 1. 注册
    username = "".join(random.choices(string.ascii_lowercase + string.digits, k=12))
    password = "Test" + "".join(random.choices(string.digits, k=8)) + "!"
    print(f"[*] 注册账号：{username} / {password}")
    
    r_reg = s.post(
        f"{BASE_URL}/api/auth/register",
        json={"username": username, "password": password, "turnstileToken": ""},
        timeout=15
    )
    print(f"[*] 注册结果：{r_reg.status_code}")
    if r_reg.status_code != 200:
        print(f"[!] 注册失败：{r_reg.text}")
        return
    
    # 2. 获取 CSRF
    r_csrf = s.get(f"{BASE_URL}/api/auth/csrf", timeout=10)
    csrf = r_csrf.json().get("csrfToken", "")
    print(f"[*] CSRF Token: {csrf[:20]}...")
    
    # 3. 登录
    r_login = s.post(
        f"{BASE_URL}/api/auth/callback/credentials",
        headers={"content-type": "application/x-www-form-urlencoded"},
        data=f"username={username}&password={password}&csrfToken={csrf}&redirect=false&callbackUrl={BASE_URL}",
        timeout=15,
        allow_redirects=True
    )
    print(f"[*] 登录结果：{r_login.status_code}")
    print(f"[*] Cookies: {[c.name for c in s.cookies]}")
    
    # 检查 session-token
    session_token = None
    for cookie in s.cookies:
        if "session-token" in cookie.name:
            session_token = cookie.value
            print(f"[*] Session Token: {session_token[:20]}...")
            break
    
    if not session_token:
        print("[!] 未获取到 session-token")
        return
    
    # 4. 获取域名配置
    r_config = s.get(f"{BASE_URL}/api/config", timeout=10)
    print(f"[*] 配置结果：{r_config.status_code}")
    print(f"[*] 配置内容：{r_config.text}")
    
    domain_str = r_config.json().get("emailDomains", "sall.cc")
    domains = [d.strip() for d in domain_str.split(",") if d.strip()]
    domain = random.choice(domains) if domains else "sall.cc"
    print(f"[*] 可用域名：{domains}")
    print(f"[*] 选择域名：{domain}")
    
    # 5. 生成邮箱
    chars = string.ascii_letters + string.digits + "_-"
    name = "".join(random.choices(chars, k=8))
    
    r_gen = s.post(
        f"{BASE_URL}/api/emails/generate",
        json={"name": name, "domain": domain, "expiryTime": 86400000},
        timeout=15
    )
    print(f"[*] 生成结果：{r_gen.status_code}")
    print(f"[*] 生成响应：{r_gen.text}")
    
    if r_gen.status_code == 401:
        print("[!] 401 错误：检查登录流程是否正确")
    elif r_gen.status_code == 200:
        data = r_gen.json()
        email_id = data.get("id")
        email_addr = data.get("address")
        print(f"[✓] 邮箱生成成功：{email_addr} (ID: {email_id})")
    else:
        print(f"[!] 未知错误：{r_gen.text}")

if __name__ == "__main__":
    test_moemail_flow()
```

## 测试检查清单

- [ ] 注册 API 返回 200
- [ ] CSRF Token 成功获取
- [ ] 登录成功，Cookie 包含 session-token
- [ ] 域名配置 API 返回 200，能解析出域名列表
- [ ] 邮箱生成 API 返回 200，返回包含 id 和 address
- [ ] 收件箱 API 能正确获取邮件列表
