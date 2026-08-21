# -*- coding: utf-8 -*-
"""密钥治理回归测试：云同步代码不得读取/上传任何用户密钥配置。

用户自己的第三方密钥（LLM / 智谱 / 云广告检测 / 语音 ASR / 独立后台令牌 /
云同步密码等）**只保留在用户本机**，不得进入 sync push 请求体，
也不得被同步代码读取后转发到服务器。

无需安装 AstrBot。
"""

import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]


def _read_source(name):
    return (ROOT / name).read_text(encoding="utf-8")


# 用户密钥配置项清单（与 _conf_schema.json 对齐）
# - FORBIDDEN：同步代码完全不得读取（这些 key 只用于本地功能，不参与云同步）
# - LOGIN_ONLY：sync_password 只允许出现在登录用户**自己服务器**的请求中（_sync_config/_sync_login），
#   绝不进入 push/pull/actions 数据体
SECRET_CFG_FORBIDDEN = [
    "cloud_audit_api_key",      # 第三方云广告检测 Key
    "zhipu_api_key_id",         # 智谱 Key
    "zhipu_api_key_secret",
    "ad_backend_token",         # 独立后台访问令牌
    "voice_asr_url",            # 语音 ASR 服务
    "umi_ocr_url",              # Umi-OCR 服务地址
]
LOGIN_ONLY_CFG = ["sync_password"]


class SyncPushNoSecretsTests(unittest.TestCase):
    """sync_client 不得读取/上传用户密钥（sync_password 仅登录用）。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("sync_client.py")

    def test_sync_client_never_reads_thirdparty_keys(self):
        # 第三方密钥/服务地址：同步代码完全不得读取
        for key in SECRET_CFG_FORBIDDEN:
            self.assertNotIn('_cfg_str("%s"' % key, self.src, key)
            self.assertNotIn('config.get("%s"' % key, self.src, key)
            self.assertNotIn('self.config["%s"]' % key, self.src, key)

    def test_sync_password_only_in_login_scope(self):
        # sync_password 只允许出现在 _sync_config/_sync_login（登录用户自己的服务器）
        cfg_seg = self.src[self.src.find("def _sync_config"):self.src.find("def _sync_client_id")]
        login_seg = self.src[self.src.find("async def _sync_login"):self.src.find("def _sync_http")]
        self.assertIn('_cfg_str("sync_password"', cfg_seg)
        self.assertIn('"password"', login_seg)

    def test_push_body_never_contains_any_secret(self):
        seg = self.src[self.src.find("async def _sync_push"):self.src.find("async def _sync_pull")]
        self.assertIn('"client_id"', seg)
        self.assertIn('"scopes"', seg)
        self.assertIn('"feedback"', seg)
        self.assertIn('"suggestions"', seg)
        self.assertIn('"violations"', seg)
        for key in SECRET_CFG_FORBIDDEN + LOGIN_ONLY_CFG:
            self.assertNotIn(key, seg, key)

    def test_pull_and_actions_never_send_secrets(self):
        seg = self.src[self.src.find("async def _sync_pull"):self.src.find("async def _sync_run")]
        for key in SECRET_CFG_FORBIDDEN + LOGIN_ONLY_CFG:
            self.assertNotIn(key, seg, key)


class SyncCollectFieldsTests(unittest.TestCase):
    """sync 收集函数的字段集合内不含任何密钥字段。"""

    @classmethod
    def setUpClass(cls):
        cls.src = _read_source("sync_client.py")

    def test_collect_fields_no_secrets(self):
        # 只检查收集函数段（_sync_collect_*），不含密钥字段
        seg = self.src[self.src.find("def _sync_collect_feedback"):self.src.find("async def _sync_push")]
        for key in SECRET_CFG_FORBIDDEN + LOGIN_ONLY_CFG:
            self.assertNotIn('"%s"' % key, seg, key)


if __name__ == "__main__":
    unittest.main()
