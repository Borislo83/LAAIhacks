// Test-only SDK boundary. Loaded only by preview-server.mjs, never the production build.
class Container { constructor(value: object) { Object.assign(this, value) } }
export { Container as TextContainerProperty, Container as ListContainerProperty,
  Container as ListItemContainerProperty, Container as ImageContainerProperty,
  Container as CreateStartUpPageContainer, Container as RebuildPageContainer,
  Container as TextContainerUpgrade, Container as ImageRawDataUpdate }
export const OsEventTypeList = { CLICK_EVENT: 0, SCROLL_TOP_EVENT: 1, SCROLL_BOTTOM_EVENT: 2,
  DOUBLE_CLICK_EVENT: 3, FOREGROUND_ENTER_EVENT: 4, FOREGROUND_EXIT_EVENT: 5, SYSTEM_EXIT_EVENT: 7, ABNORMAL_EXIT_EVENT: 8 }
export async function waitForEvenAppBridge() {
  // Visible test controls drive the real main.ts event handler and expose what
  // crossed the SDK boundary, independently of the phone's mirror rendering.
  let listener: (event: any) => void = () => {}
  let texts: any[] = []
  let lists: any[] = []
  let nativeIndex = 0
  let shutdowns = 0
  const panel = document.createElement('details')
  panel.style.cssText = 'max-width:600px;margin:16px auto;padding:16px;border:1px solid #888'
  panel.innerHTML = '<summary>Glasses test controls</summary><div data-controls></div><pre data-device style="white-space:pre-wrap;font-size:12px"></pre>'
  document.body.append(panel)
  const output = panel.querySelector('[data-device]')!
  const update = () => { output.textContent = `Shutdown requests: ${shutdowns}\n` + texts.map(t =>
    `[${t.containerID}: ${t.xPosition},${t.yPosition} ${t.width}x${t.height}] ${t.content}`).join('\n') +
    lists.map(list => '\n' + list.itemContainer.itemName.map((name: string, index: number) => `${index === nativeIndex ? '> ' : '  '}${name}`).join('\n')).join('') }
  for (const [label, type] of [['Device swipe up', 1], ['Device swipe down', 2], ['Device tap', 0], ['Device double tap', 3]] as const) {
    const button = document.createElement('button')
    button.type = 'button'; button.textContent = label
    button.addEventListener('click', () => {
      if (lists.length) {
        if (type === 1 || type === 2) nativeIndex = Math.max(0, Math.min(lists[0].itemContainer.itemCount - 1, nativeIndex + (type === 1 ? -1 : 1)))
        update()
        listener({ listEvent: { eventType: type, currentSelectItemIndex: nativeIndex } })
      } else listener({ textEvent: { eventType: type } })
    })
    panel.querySelector('[data-controls]')!.append(button)
  }
  return {
    async createStartUpPageContainer(page: any) { texts = page.textObject.map((t: any) => ({ ...t })); update(); return 0 },
    async rebuildPageContainer(page: any) {
      texts = page.textObject.map((t: any) => ({ ...t })); lists = page.listObject ?? []; nativeIndex = 0; update(); return 0
    },
    async textContainerUpgrade(value: any) {
      const text = texts.find(t => t.containerID === value.containerID)
      if (!text) throw new Error(`Unknown device text container ${value.containerID}`)
      text.content = value.content; update(); return 0
    }, async updateImageRawData() { return 'success' },
    async shutDownPageContainer() { shutdowns++; update(); return 0 },
    async setLocalStorage(key: string, value: string) { sessionStorage.setItem(key, value) },
    async getLocalStorage(key: string) { return sessionStorage.getItem(key) },
    onEvenHubEvent(callback: typeof listener) { listener = callback; return () => { listener = () => {} } },
  }
}
