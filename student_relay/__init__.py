"""暴雨预警下寄宿学校跨校区转运与接领后端服务。

模块划分：
- storage: SQLite 持久化与建表
- auth: 令牌与岗位权限
- domain: 照管状态机、交接、授权核验、点名差异等业务规则
- partner: 通过项目自带对端接口收发门岗证据（含断连补传）
- httpapi: HTTP JSON 接口
"""

__all__ = ["storage", "auth", "domain", "partner", "httpapi"]
