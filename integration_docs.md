# 库存预警 Webhook 与自动导入对接文档

本文档用于指导开发者编写对接程序，实现在收到库存预警通知后自动导入新账号的功能。

## 1. 库存预警 Webhook 通知

当系统内所有活跃 Team 的总剩余车位（`max_members - current_members`）数量低于或等于管理员设置的阈值时，系统会向配置的 Webhook URL 发送 POST 请求。

### 请求信息
- **方法**: `POST`
- **Content-Type**: `application/json`

### 请求 Payload 示例
```json
{
    "event": "low_stock",
    "current_seats": 5,
    "threshold": 10,
    "message": "库存不足预警：系统总可用车位仅剩 5，已低于预警阈值 10，请及时补货导入新账号。"
}
```

---

## 2. 账号自动导入接口

对接程序在收到通知并准备好新账号数据后，可以调用以下接口进行导入。

### 接口信息
- **接口地址**: `/admin/teams/import`
- **方法**: `POST`
- **认证方式**:
  1. **Session 认证**: 浏览器访问时自动使用。
  2. **API Key 认证**: 对接程序建议使用此方式。在 `Header` 中添加 `X-API-Key`。
- **配置位置**: 管理员后台 -> 系统设置 -> 库存预警 Webhook -> API Key。

### 导入模式 A：单账号导入 (Single)
适用于逐个导入账号。

**认证逻辑（三选一）**:
- 提供 `access_token`: 最直接的方式。
- 提供 `session_token`: 如果 AT 缺失，系统会尝试用 ST 刷新获取 AT。
- 提供 `refresh_token` + `client_id`: 如果上述皆无，系统尝试用 RT 刷新。

**Payload 结构**:
| 字段 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `import_type` | string | **是** | 固定为 `"single"` |
| `access_token` | string | 建议 | ChatGPT 的 Access Token (AT) |
| `session_token` | string | 建议 | 用于自动刷新 AT 的 Session Token (ST) |
| `email` | string | 否 | 账号邮箱。若不填，系统将尝试从 AT 中解析。 |
| `account_id` | string | 否 | Team 的 Account ID。若不填，系统将自动获取该账号下所有活跃的 Team。 |
| `refresh_token`| string | 否 | 用于刷新的 Refresh Token (RT) |
| `client_id` | string | 否 | 配合 RT 使用的 Client ID |

---

### 导入模式 B：批量导入 (Batch)
适用于一次性导入多个账号，系统会自动解析文本中的信息。

**Payload 结构**:
| 字段 | 类型 | 必填 | 说明 |
| :--- | :--- | :--- | :--- |
| `import_type` | string | **是** | 固定为 `"batch"` |
| `content` | string | **是** | 包含账号信息的文本内容 |

**批量导入格式说明**:
支持多种分隔符（如 `,` 或 `----`）。通常每一行代表一个账号，格式建议为：
`邮箱,Access_Token,Refresh_Token,Session_Token,Client_ID`
*(注：如果某列缺失可以用空占位，如 `email,at,,,`)*

---

## 3. 实现建议 (Python 示例)

```python
import httpx
from fastapi import FastAPI, Request

app = FastAPI()

# 这里的 API Key 需要与管理系统“系统设置”中配置的一致
API_KEY = "YOUR_CONFIGURED_API_KEY"
ADMIN_API_URL = "http://your-manager-domain.com/admin/teams/import"

@app.post("/webhook/low-stock")
async def handle_low_stock(request: Request):
    data = await request.json()
    print(f"收到预警: {data['message']}")
    
    # 逻辑：从其它来源获取新账号数据
    # ...获取逻辑...
    
    new_account = {
        "import_type": "single",
        "email": "new_team@example.com",
        "access_token": "NEW_ACCESS_TOKEN"
    }
    
    # 调用管理系统导入接口
    async with httpx.AsyncClient() as client:
        # 使用 X-API-Key 进行身份验证
        response = await client.post(
            ADMIN_API_URL,
            json=new_account,
            headers={"X-API-Key": API_KEY}
        )
        print(f"导入结果: {response.json()}")
    
    return {"status": "ok"}
```
