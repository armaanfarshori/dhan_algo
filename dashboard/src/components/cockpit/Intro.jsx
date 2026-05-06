import { useEffect, useState } from 'react'
import { T } from '../../tokens'

export default function Intro() {
  const [gone, setGone] = useState(false)
  const [removed, setRemoved] = useState(false)

  useEffect(() => {
    const t1 = setTimeout(() => setGone(true), 2200)
    const t2 = setTimeout(() => setRemoved(true), 2900)
    return () => { clearTimeout(t1); clearTimeout(t2) }
  }, [])

  if (removed) return null

  return (
    <div style={{
      position:'fixed', inset:0, background:T.bg0, zIndex:50,
      display:'flex', alignItems:'center', justifyContent:'center',
      transition:'opacity 0.6s ease', opacity: gone ? 0 : 1,
      pointerEvents:'none',
    }}>
      <div style={{
        position:'absolute', left:'50%', top:0, width:1, height:'100vh',
        background:T.green, opacity:0.3,
        animation:'introCross 1.2s ease forwards',
      }} />
      <div style={{
        position:'absolute', top:'50%', left:0, width:'100vw', height:1,
        background:T.green, opacity:0.3,
        animation:'introCross 1.2s ease forwards',
      }} />
      <div style={{
        fontFamily:T.dot, fontSize:80, color:T.green,
        textShadow:`0 0 30px oklch(0.78 0.19 145 / 0.4)`,
        animation:'introWord 1.6s ease forwards', opacity:0,
      }}>
        DHAN · ALGO
      </div>
    </div>
  )
}
