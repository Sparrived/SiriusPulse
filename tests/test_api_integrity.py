"""
API 层隐藏性与完整性自动化测试

运行方式: pytest tests/test_api_integrity.py -v
"""

import pytest
import sys
import warnings
from typing import Set


class TestAPILayerHiding:
    """验证 API 层隐藏所有内部实现"""
    
    @pytest.fixture(autouse=True)
    def setup(self):
        """设置测试环境"""
        # 清空已导入的 sirius_pulse，确保新鲜导入
        if 'sirius_pulse' in sys.modules:
            del sys.modules['sirius_pulse']
    
    def test_api_has_all_defined(self):
        """测试：公开API应该在 __all__ 中定义"""
        import sirius_pulse
        
        assert hasattr(sirius_pulse, '__all__'), \
            "sirius_pulse 必须定义 __all__"
        assert isinstance(sirius_pulse.__all__, (list, tuple)), \
            "__all__ 必须是列表或元组"
        assert len(sirius_pulse.__all__) >= 10, \
            "__all__ 中应该至少有 10 个导出"
    
    def test_no_private_members_leaked(self):
        """测试：不应该导出以 _ 开头的私有成员"""
        import sirius_pulse
        
        allowed_private = {
            '__package__', '__name__', '__doc__', '__file__',
            '__loader__', '__spec__', '__cached__', '__builtins__',
            '__version__', '__all__', '__annotations__'
        }
        
        leaked = []
        for name in dir(sirius_pulse):
            if name.startswith('_') and name not in allowed_private:
                # 检查是否在 __all__ 中
                if name in sirius_pulse.__all__:
                    leaked.append(name)
        
        assert not leaked, f"私有实现泄露: {leaked}"
    
    def test_no_internal_packages_exposed(self):
        """测试：内部包（core, memory 等）不应该直接暴露"""
        import sirius_pulse
        import inspect
        
        # 这些包应该是内部实现，不应该直接导出
        internal_packages = {
            'core', 'memory', 'config',
            'session', 'token'
        }
        
        exposed = []
        for name in sirius_pulse.__all__:
            obj = getattr(sirius_pulse, name)
            if inspect.ismodule(obj):
                pkg_name = obj.__name__.split('.')[-1]
                if pkg_name in internal_packages:
                    exposed.append(name)
        
        assert not exposed, f"内部包暴露: {exposed}"
    
    def test_all_exported_are_accessible(self):
        """测试：__all__ 中的所有项都应该可访问"""
        import sirius_pulse
        
        for name in sirius_pulse.__all__:
            assert hasattr(sirius_pulse, name), \
                f"__all__ 中的 {name} 无法访问"
            
            obj = getattr(sirius_pulse, name)
            assert obj is not None, \
                f"__all__ 中的 {name} 为 None"
    
    def test_required_api_exported(self):
        """测试：必需的公开API都应该被导出"""
        import sirius_pulse
        
        required = {
            'EmotionalGroupChatEngine',
            'SessionConfig',
            'OrchestrationPolicy',
            'UserProfile',
            'Message',
            'Transcript',
            'IdentityResolver',
            'IdentityContext',
        }
        
        all_set = set(sirius_pulse.__all__)
        missing = required - all_set
        
        assert not missing, f"缺少必需的API: {missing}"
    
    def test_all_documented(self):
        """测试：所有导出的类都应该有文档"""
        import sirius_pulse
        import inspect
        
        undocumented = []
        for name in sirius_pulse.__all__:
            obj = getattr(sirius_pulse, name)
            
            # 检查类或函数是否有文档
            if inspect.isclass(obj) or inspect.isfunction(obj):
                if not obj.__doc__ or len(obj.__doc__.strip()) < 10:
                    undocumented.append(name)
        
        # 允许一些数据对象（如 TRAIT_TAXONOMY）没有 __doc__
        # 但大多数类应该有文档
        class_count = sum(1 for n in sirius_pulse.__all__
                         if inspect.isclass(getattr(sirius_pulse, n)))
        undoc_count = len(undocumented)
        
        # 至少 80% 的类应该有文档
        if class_count > 0:
            doc_ratio = (class_count - undoc_count) / class_count
            assert doc_ratio >= 0.8, \
                f"文档不完整: {undoc_count}/{class_count} 个类缺少文档"


class TestAPIFunctionality:
    """验证通过公开API能完整使用库"""
    
    def test_can_create_session_config(self):
        """测试：能否通过公开API创建会话配置"""
        from sirius_pulse import SessionConfig, AgentPreset, Agent
        from pathlib import Path
        
        # SessionConfig 需要 work_path 和 preset
        agent = Agent(name="assistant", persona="helpful", model="test")
        preset = AgentPreset(agent=agent, global_system_prompt="test")
        config = SessionConfig(
            work_path=Path("/tmp"),
            preset=preset
        )
        
        assert config.preset.agent.name == "assistant"
        assert config.work_path == Path("/tmp")
    
    def test_can_create_user_profile(self):
        """测试：能否通过公开API创建用户档案"""
        from sirius_pulse import UserProfile
        
        profile = UserProfile(
            user_id="test_user",
            name="Test User"
        )
        
        assert profile.user_id == "test_user"
        assert profile.name == "Test User"
    
    def test_can_create_identity_resolver(self):
        """测试：能否创建身份解析器"""
        from sirius_pulse import IdentityResolver, IdentityContext
        
        resolver = IdentityResolver()
        
        ctx = IdentityContext(
            speaker_name="Test",
            user_id="test_user"
        )
        
        assert ctx.speaker_name == "Test"
        assert ctx.user_id == "test_user"
    
    def test_cannot_access_internal_functions(self):
        """测试：不应该能访问内部函数"""
        import sirius_pulse
        
        # 这些是内部实现，不应该导出
        internal_functions = [
            '_normalize_trait',
            '_extract_keywords',
            '_merge_unique',
            '_score'
        ]
        
        for func_name in internal_functions:
            # 尝试从顶级 API 导入（应该失败）
            try:
                getattr(sirius_pulse, func_name)
                # 如果找到了，说明泄露了
                pytest.fail(f"内部函数 {func_name} 不应该被导出")
            except AttributeError:
                # 这是预期的！
                pass
    
    def test_cannot_access_internal_classes(self):
        """测试：不应该能访问内部类"""
        import sirius_pulse
        
        # 这些是内部实现，不应该导出
        internal_classes = [
            '_ConversationState',
            '_EventMemoryEntry',
            '_MemoryQualityMetrics'
        ]
        
        for class_name in internal_classes:
            try:
                getattr(sirius_pulse, class_name)
                # 如果找到了，说明泄露了
                pytest.fail(f"内部类 {class_name} 不应该被导出")
            except AttributeError:
                # 这是预期的！
                pass


class TestAPIDataIntegrity:
    """验证数据模型和API数据完整性"""
    
    def test_message_model_available(self):
        """测试：Message 数据模型应该可用"""
        from sirius_pulse import Message
        
        msg = Message(
            role="user",
            content="Hello"
        )
        
        assert msg.role == "user"
        assert msg.content == "Hello"
    
    def test_transcript_model_available(self):
        """测试：Transcript 数据模型应该可用"""
        from sirius_pulse import Transcript, Message
        
        msg = Message(role="user", content="Hi")
        transcript = Transcript(messages=[msg])
        
        assert len(transcript.messages) == 1
        assert transcript.messages[0].role == "user"
    
    def test_identity_context_available(self):
        """测试：身份上下文应该可用"""
        from sirius_pulse import IdentityContext
        
        ctx = IdentityContext(
            speaker_name="Alice",
            user_id="u123",
            platform_uid="qq_456",
            platform="qq",
            is_developer=False
        )
        
        assert ctx.speaker_name == "Alice"
        assert ctx.platform == "qq"
        assert not ctx.is_developer


if __name__ == "__main__":
    pytest.main([__file__, "-v"])
