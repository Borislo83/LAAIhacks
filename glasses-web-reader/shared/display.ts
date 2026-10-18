// "Hey Focal, hide the display" blanks the lens; "show the display" (or any tap or swipe)
// brings back what was there.

const PREFIX = /^(?:(?:hey|hi|ok|okay|please|can you|could you|focal|so|um)[\s,]+)*/
const THE = '(?:the\\s+|my\\s+|your\\s+)?'
const SCREEN = '(?:display|screen|lens|lenses|glasses|hud|overlay|everything|it)'
const HIDE = new RegExp(`^(?:hide|clear|blank|turn\\s+off|switch\\s+off|close|dim)\\s+${THE}${SCREEN}$|^(?:${THE}${SCREEN}\\s+off|go\\s+(?:dark|blank)|blank\\s+out)$`)
const SHOW = new RegExp(`^(?:show|unhide|bring\\s+back|turn\\s+on|switch\\s+on|wake\\s+up|open)\\s+${THE}${SCREEN}(?:\\s+again)?$|^(?:${THE}${SCREEN}\\s+on|unhide|wake\\s+up)$`)

/** 'hide' or 'show' for a spoken display command, else null. */
export function displayCommand(command: string): 'hide' | 'show' | null {
  const said = command.toLowerCase().replace(/[’']/g, '').replace(/[^a-z\s,]+/g, ' ').replace(/\s+/g, ' ').trim()
    .replace(PREFIX, '').replace(/[\s,]+please$/, '').replace(/,/g, '').trim()
  if (HIDE.test(said)) return 'hide'
  if (SHOW.test(said)) return 'show'
  return null
}
