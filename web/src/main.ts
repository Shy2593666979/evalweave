import { createPinia } from 'pinia'
import { createApp } from 'vue'
import 'element-plus/dist/index.css'
import './styles.css'
import App from './App.vue'
import router from './router'

const pinia = createPinia()
createApp(App).use(pinia).use(router).mount('#app')
