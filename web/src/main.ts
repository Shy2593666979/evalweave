import { createPinia } from 'pinia'
import { createApp } from 'vue'
import { ElLoading } from 'element-plus'
import 'element-plus/dist/index.css'
import './styles.css'
import App from './App.vue'
import router from './router'

const pinia = createPinia()
const app = createApp(App)

app.directive('loading', ElLoading.directive)
app.use(pinia).use(router).mount('#app')
