<script setup lang="ts">
import axios from 'axios'
import { ElButton, ElTag } from 'element-plus'
import { onMounted, ref } from 'vue'

type Health = { status: string; name: string; version: string }

const health = ref<Health | null>(null)
const backendReachable = ref(false)

onMounted(async () => {
  try {
    const response = await axios.get<Health>('/api/health')
    health.value = response.data
    backendReachable.value = true
  } catch {
    backendReachable.value = false
  }
})
</script>

<template>
  <header class="page-header">
    <div>
      <p class="eyebrow">WORKSPACE</p>
      <h1>评测工作台</h1>
      <p>让 AI 的每一步，都可以被度量。</p>
    </div>
    <el-button type="primary" disabled>创建实验</el-button>
  </header>

  <section class="status-card">
    <div>
      <span class="status-dot" :class="{ online: backendReachable }" />
      <strong>{{ backendReachable ? '服务运行正常' : '等待后端服务' }}</strong>
      <p v-if="health">{{ health.name }} v{{ health.version }}</p>
      <p v-else>启动 API 后，这里会显示实时状态。</p>
    </div>
    <el-tag :type="backendReachable ? 'success' : 'info'">
      {{ backendReachable ? 'Online' : 'Offline' }}
    </el-tag>
  </section>

  <section class="metric-grid">
    <article><span>数据集</span><strong>0</strong><small>等待导入</small></article>
    <article><span>实验运行</span><strong>0</strong><small>暂无运行</small></article>
    <article><span>测试用例</span><strong>0</strong><small>暂无用例</small></article>
    <article><span>平均得分</span><strong>—</strong><small>暂无结果</small></article>
  </section>

  <section class="empty-panel">
    <div class="empty-icon">⌁</div>
    <h2>从第一个数据集开始</h2>
    <p>导入 JSON、JSONL、CSV 或 Excel，创建可重复运行的 AI 评测实验。</p>
    <el-button disabled>导入数据集</el-button>
  </section>
</template>
