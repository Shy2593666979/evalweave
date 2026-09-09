from enum import StrEnum


class Permission(StrEnum):
    PROJECT_READ = "project:read"
    PROJECT_WRITE = "project:write"
    DATASET_READ = "dataset:read"
    DATASET_WRITE = "dataset:write"
    EXPERIMENT_READ = "experiment:read"
    EXPERIMENT_RUN = "experiment:run"
    EVALUATION_REVIEW = "evaluation:review"
    USER_MANAGE = "user:manage"
    USER_TYPE_MANAGE = "user_type:manage"


DEFAULT_USER_TYPES = [
    {
        "code": "product",
        "name": "产品同学",
        "description": "管理评测需求、查看数据和运行实验",
        "permissions": [
            Permission.PROJECT_READ,
            Permission.DATASET_READ,
            Permission.EXPERIMENT_READ,
            Permission.EXPERIMENT_RUN,
            Permission.EVALUATION_REVIEW,
        ],
    },
    {
        "code": "research",
        "name": "用研同学",
        "description": "查看实验结果并参与人工评审",
        "permissions": [
            Permission.PROJECT_READ,
            Permission.DATASET_READ,
            Permission.EXPERIMENT_READ,
            Permission.EVALUATION_REVIEW,
        ],
    },
    {
        "code": "development",
        "name": "研发同学",
        "description": "维护数据集、执行实验和分析结果",
        "permissions": [
            Permission.PROJECT_READ,
            Permission.PROJECT_WRITE,
            Permission.DATASET_READ,
            Permission.DATASET_WRITE,
            Permission.EXPERIMENT_READ,
            Permission.EXPERIMENT_RUN,
            Permission.EVALUATION_REVIEW,
        ],
    },
    {
        "code": "testing",
        "name": "测试同学",
        "description": "维护测试数据、执行实验和复核结果",
        "permissions": [
            Permission.PROJECT_READ,
            Permission.DATASET_READ,
            Permission.DATASET_WRITE,
            Permission.EXPERIMENT_READ,
            Permission.EXPERIMENT_RUN,
            Permission.EVALUATION_REVIEW,
        ],
    },
]


def permission_catalog() -> list[dict[str, str]]:
    labels = {
        Permission.PROJECT_READ: "查看项目",
        Permission.PROJECT_WRITE: "管理项目",
        Permission.DATASET_READ: "查看数据集",
        Permission.DATASET_WRITE: "管理数据集",
        Permission.EXPERIMENT_READ: "查看实验",
        Permission.EXPERIMENT_RUN: "运行实验",
        Permission.EVALUATION_REVIEW: "人工评审",
        Permission.USER_MANAGE: "管理用户",
        Permission.USER_TYPE_MANAGE: "管理用户类型",
    }
    return [{"key": item.value, "label": labels[item]} for item in Permission]
