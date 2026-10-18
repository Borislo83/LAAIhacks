/* Agent orbs and the aperture logo, shared by focal.html and dashboard.html.
   A classic script (not a module) so focal.html still works when opened as a file. */
function orb(v,size=40,idle=false){
  const id="c"+(orb.uid++), r=size/2-1, c=size/2, g="#4A4A50";
  let el="";
  const st=Math.max(2.6,size/11), n=Math.floor(size/st)+1;
  if(v===1){const gap=Math.max(3,Math.round(size/12));let i=0;for(let y=2;y<size-1;y+=gap,i++)el+=`<rect class="s1" style="animation-delay:${(i*0.09).toFixed(2)}s" x="0" y="${y}" width="${size}" height="${Math.max(1.2,size/18).toFixed(1)}" fill="#fff"/>`}
  if(v===2||v===4)for(let i=0;i<n;i++)el+=`<rect x="${(i*st+1).toFixed(1)}" y="0" width="1" height="${size}" fill="${g}"/>`;
  if(v===2)for(let k=0;k<5;k++)el+=`<circle class="s2" style="animation-delay:${(-k*0.32).toFixed(2)}s" cx="${(size*.15+k*size*.175).toFixed(1)}" cy="${c}" r="${Math.max(1,size/22).toFixed(1)}" fill="#fff"/>`;
  if(v===4)[[.3,.25,.22],[.48,.45,.18],[.62,.3,.12],[.72,.6,.2],[.2,.55,.14]].forEach(([x,y,h],k)=>el+=`<rect class="s4" style="animation-delay:${(k*0.17).toFixed(2)}s" x="${(x*size).toFixed(1)}" y="${(y*size).toFixed(1)}" width="${Math.max(1,size/16).toFixed(1)}" height="${(h*size).toFixed(1)}" fill="#fff"/>`);
  if(v===5){for(let i=0;i<n;i++)el+=`<rect x="0" y="${(i*st+1).toFixed(1)}" width="${size}" height="1" fill="${g}"/>`;
    const t=size*.2;el+=`<path class="s5" d="M${(c-t*.75).toFixed(1)} ${(c-t).toFixed(1)}L${(c+t).toFixed(1)} ${c}L${(c-t*.75).toFixed(1)} ${(c+t).toFixed(1)}Z" fill="#fff"/>`}
  if(v===6){for(let i=0;i<n;i++)el+=`<rect x="${(i*st+1).toFixed(1)}" y="0" width="1" height="${size}" fill="${g}"/>`;
    for(let k=0;k<3;k++)el+=`<circle class="s6" style="animation-delay:${(k*0.18).toFixed(2)}s" cx="${(c+(k-1)*size*.2).toFixed(1)}" cy="${c}" r="${Math.max(1.2,size/14).toFixed(1)}" fill="#fff"/>`}
  if(v===7){for(let i=0;i<n;i++)el+=`<rect x="0" y="${(i*st+1).toFixed(1)}" width="${size}" height="1" fill="${g}"/>`;
    const b=Math.max(1.2,size/14);el+=`<rect x="${(size*.2).toFixed(1)}" y="${(size*.3).toFixed(1)}" width="${(size*.22).toFixed(1)}" height="${(size*.2).toFixed(1)}" fill="#fff" opacity=".85"/>`;
    [[.48,.3,.32],[.48,.42,.26],[.2,.58,.6],[.2,.7,.48]].forEach(([x,y,w],k)=>el+=`<rect class="s7" style="animation-delay:${(k*0.22).toFixed(2)}s" x="${(x*size).toFixed(1)}" y="${(y*size).toFixed(1)}" width="${(w*size).toFixed(1)}" height="${b.toFixed(1)}" fill="#fff"/>`)}
  // Verifier: a tick draws itself over scanlines.
  if(v===8){for(let i=0;i<n;i++)el+=`<rect x="0" y="${(i*st+1).toFixed(1)}" width="${size}" height="1" fill="${g}"/>`;
    el+=`<path class="s8" pathLength="1" d="M${(size*.28).toFixed(1)} ${(size*.52).toFixed(1)}L${(size*.44).toFixed(1)} ${(size*.68).toFixed(1)}L${(size*.74).toFixed(1)} ${(size*.34).toFixed(1)}" fill="none" stroke="#fff" stroke-width="${Math.max(1.4,size/13).toFixed(1)}" stroke-linecap="round" stroke-linejoin="round"/>`}
  // Memory: stored records light up one after another.
  if(v===9){for(let i=0;i<n;i++)el+=`<rect x="${(i*st+1).toFixed(1)}" y="0" width="1" height="${size}" fill="${g}"/>`;
    const h=Math.max(1.6,size/11);[.3,.46,.62].forEach((y,k)=>el+=`<rect class="s9" style="animation-delay:${(k*0.35).toFixed(2)}s" x="${(size*.25).toFixed(1)}" y="${(y*size).toFixed(1)}" width="${(size*.5).toFixed(1)}" height="${h.toFixed(1)}" rx="${(h/2).toFixed(1)}" fill="#fff"/>`)}
  if(v===3){for(let i=0;i<n;i++)el+=`<rect x="0" y="${(i*st+1).toFixed(1)}" width="${size}" height="1" fill="${g}"/>`;
    [[.28,.3,1.9],[.45,.25,2.6],[.62,.2,2.2],[.8,.15,3.1]].forEach(([y,w,d],k)=>el+=`<rect class="s3" style="animation-duration:${d}s;animation-delay:${(-k*0.7).toFixed(2)}s" x="0" y="${(y*size).toFixed(1)}" width="${(w*size).toFixed(1)}" height="${Math.max(1,size/16).toFixed(1)}" fill="#fff"/>`)}
  return `<svg class="orb${idle?" idle":""}" width="${size}" height="${size}" viewBox="0 0 ${size} ${size}" aria-hidden="true"><defs><clipPath id="${id}"><circle cx="${c}" cy="${c}" r="${r}"/></clipPath></defs><circle cx="${c}" cy="${c}" r="${r}" fill="#0E0E10"/><g clip-path="url(#${id})">${el}</g></svg>`;
}
orb.uid=0;
function logoMark(size=22,cls=""){
  return `<svg${cls?` class="${cls}"`:""} width="${size}" height="${size}" viewBox="0 0 22 22" aria-hidden="true"><circle cx="11" cy="11" r="9.5" fill="none" stroke="#0B0B0C" stroke-width="2"/><circle cx="11" cy="11" r="3.5" fill="#2F47E8"/></svg>`;
}
