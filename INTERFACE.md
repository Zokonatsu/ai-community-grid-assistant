# 模块接口约定

## 接收Agent (receive_agent.py)
输入: {"description": "居民原始描述"}
输出: {
    "address": "具体地址",
    "event_type": "物业维修/环境卫生/安全隐患/邻里纠纷/公共设施/其他",
    "urgency": "高/中/低",
    "handler": ""  // 初始为空，由派发Agent填充
}

## 派发Agent (dispatch_agent.py)
输入: 接收Agent的完整输出
输出: {
    "handler": "物业/城管/消防/调解员/社区"
}

## 规则
- 接收Agent只负责提取信息，不决定派给谁
- 派发Agent只负责匹配处理方，不修改其他字段
- 两个Agent通过State共享数据