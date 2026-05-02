# MoeMail 邮箱地址生成规则分析

## 1. 接口位置

- **API 路由**: `/tmp/moemail/app/api/emails/generate/route.ts`
- **请求方法**: `POST`
- **运行环境**: Edge Runtime (`export const runtime = "edge"`)

## 2. 邮箱地址生成算法

### 2.1 生成公式

```typescript
const address = `${name || nanoid(8)}@${domain}`
```

邮箱地址由两部分组成：
- **前缀**: 用户自定义名称 或 8 位随机字符串
- **域名**: 从配置的可信域名列表中选择

### 2.2 随机字符串生成

- **使用的库**: `nanoid` v5.0.6
- **默认长度**: 8 个字符
- **字符集**: nanoid 默认使用 URL 安全的字符集（`A-Za-z0-9_-`）
- **使用场景**: 
  - 当用户未提供自定义名称时，自动生成随机前缀
  - 前端提供"刷新"按钮，用户可主动生成随机名称

### 2.3 前端随机名称生成

```typescript
// /tmp/moemail/app/components/emails/create-dialog.tsx
const generateRandomName = () => setEmailName(nanoid(8))
```

## 3. 域名配置方式

### 3.1 配置来源

域名配置存储在 Cloudflare KV 存储 `SITE_CONFIG` 中：

```typescript
// 获取配置
const domainString = await env.SITE_CONFIG.get("EMAIL_DOMAINS")
const domains = domainString ? domainString.split(',') : ["moemail.app"]
```

### 3.2 配置接口

- **读取**: `GET /api/config`
- **修改**: `POST /api/config` (需要 `MANAGE_CONFIG` 权限)
- **默认值**: `moemail.app`
- **格式**: 逗号分隔的字符串，如 `"moemail.app,example.com,tempmail.org"`

### 3.3 前端域名选择

前端从配置中获取域名列表，当有多个域名时显示下拉选择器：

```typescript
// 仅当有多个域名时显示选择器
{(config?.emailDomainsArray?.length ?? 0) > 1 && (
  <Select value={currentDomain} onValueChange={setCurrentDomain}>
    {config?.emailDomainsArray?.map(d => (
      <SelectItem key={d} value={d}>@{d}</SelectItem>
    ))}
  </SelectContent>
)}
```

## 4. 自定义选项

### 4.1 用户可自定义的参数

| 参数 | 类型 | 说明 | 是否必填 |
|------|------|------|----------|
| `name` | string | 邮箱前缀名称 | 否（不填则随机生成） |
| `domain` | string | 邮箱域名 | 是（从配置列表选择） |
| `expiryTime` | number | 过期时间（毫秒） | 是 |

### 4.2 过期时间选项

```typescript
// /tmp/moemail/app/types/email.ts
export const EXPIRY_OPTIONS: ExpiryOption[] = [
  { label: '1 小时', value: 1000 * 60 * 60 },
  { label: '24 小时', value: 1000 * 60 * 60 * 24 },
  { label: '3 天', value: 1000 * 60 * 60 * 24 * 3 },
  { label: '永久', value: 0 }
]
```

### 4.3 权限控制

- **EMPEROR 角色**: 无邮箱数量限制
- **其他角色**: 受 `MAX_EMAILS` 配置限制（默认 30 个活跃邮箱）

## 5. 冲突检测

### 5.1 检测逻辑

```typescript
const existingEmail = await db.query.emails.findFirst({
  where: eq(sql`LOWER(${emails.address})`, address.toLowerCase())
})

if (existingEmail) {
  return NextResponse.json(
    { error: "该邮箱地址已被使用" },
    { status: 409 }
  )
}
```

### 5.2 检测特点

- **大小写不敏感**: 使用 `LOWER()` 函数进行规范化比较
- **数据库级别检查**: 在插入前查询数据库确认唯一性
- **错误处理**: 返回 HTTP 409 Conflict 状态码

## 6. 完整请求流程

```
1. 用户输入邮箱名称（可选）
   ↓
2. 选择域名（从配置列表）
   ↓
3. 选择过期时间
   ↓
4. 前端调用 POST /api/emails/generate
   ↓
5. 后端验证：
   - 用户角色和邮箱数量限制
   - 过期时间有效性
   - 域名是否在允许列表中
   ↓
6. 生成邮箱地址：name || nanoid(8) + @ + domain
   ↓
7. 冲突检测（大小写不敏感）
   ↓
8. 插入数据库
   ↓
9. 返回邮箱 ID 和地址
```

## 7. 关键代码片段

### 7.1 核心生成逻辑

```typescript
// /tmp/moemail/app/api/emails/generate/route.ts

// 1. 获取可用域名列表
const domainString = await env.SITE_CONFIG.get("EMAIL_DOMAINS")
const domains = domainString ? domainString.split(',') : ["moemail.app"]

// 2. 验证域名
if (!domains || !domains.includes(domain)) {
  return NextResponse.json({ error: "无效的域名" }, { status: 400 })
}

// 3. 生成邮箱地址
const address = `${name || nanoid(8)}@${domain}`

// 4. 冲突检测
const existingEmail = await db.query.emails.findFirst({
  where: eq(sql`LOWER(${emails.address})`, address.toLowerCase())
})

// 5. 插入数据库
const result = await db.insert(emails)
  .values(emailData)
  .returning({ id: emails.id, address: emails.address })
```

## 8. 安全考虑

1. **域名白名单**: 只能使用配置中允许的域名
2. **唯一性保证**: 数据库级别的大小写不敏感唯一性检查
3. **数量限制**: 防止用户滥用（EMPEROR 除外）
4. **过期机制**: 邮箱可设置过期时间，自动失效

## 9. 依赖项

```json
{
  "nanoid": "^5.0.6",
  "drizzle-orm": "*",
  "@cloudflare/next-on-pages": "*",
  "next": "15.1.1"
}
```

## 10. 总结

MoeMail 的邮箱生成规则简洁明了：

- **格式**: `<前缀>@<域名>`
- **前缀**: 用户自定义或 8 位 nanoid 随机字符串
- **域名**: 从 SITE_CONFIG.EMAIL_DOMAINS 配置中选择
- **唯一性**: 大小写不敏感的数据库级冲突检测
- **扩展性**: 支持多域名配置，管理员可动态添加/修改可用域名
