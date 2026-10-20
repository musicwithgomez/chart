// Shared by index / admin / trader / investor pages: Firebase + role logic in ONE place.
import{initializeApp}from"https://www.gstatic.com/firebasejs/10.14.1/firebase-app.js";
import{getAuth,onAuthStateChanged,signOut}from"https://www.gstatic.com/firebasejs/10.14.1/firebase-auth.js";
import*as F from"https://www.gstatic.com/firebasejs/10.14.1/firebase-firestore.js";
const app=initializeApp({apiKey:"AIzaSyBDnlqxra1KMnuNb_5b32lYbl1-TOKmNDU",authDomain:"gomeztrading-60c2d.firebaseapp.com",projectId:"gomeztrading-60c2d",storageBucket:"gomeztrading-60c2d.firebasestorage.app",messagingSenderId:"348445408639",appId:"1:348445408639:web:b68630c3e3ed858231ee4a"});
export const auth=getAuth(app),db=F.getFirestore(app);export{F};
export const HOME={admin:"admin.html",trader:"trader.html",investor:"investor.html"};
export const esc=s=>String(s??"").replace(/[&<>"']/g,c=>({"&":"&amp;","<":"&lt;",">":"&gt;",'"':"&quot;","'":"&#39;"}[c]));
export const money=n=>"$"+(+n||0).toLocaleString(undefined,{maximumFractionDigits:0});
export const logout=()=>signOut(auth).then(()=>location.replace("login.html"));
// admin = a doc in /admins/{uid} (you create it by hand, nobody can self-promote). Else trader, else investor. Old "client" = investor.
export async function whoAmI(u){
 const[a,p]=await Promise.all([F.getDoc(F.doc(db,"admins",u.uid)),F.getDoc(F.doc(db,"users",u.uid))]);
 const d=p.exists()?p.data():{},r=(d.roles||[]).map(x=>x==="client"?"investor":x);
 return{u,uid:u.uid,data:d,blocked:d.status==="blocked",role:a.exists()?"admin":r.includes("trader")?"trader":"investor"};
}
const user=()=>new Promise(r=>{const o=onAuthStateChanged(auth,u=>{o();r(u)})});
// Page guard: not signed in -> login; wrong role -> your own dashboard.
export async function guard(allowed){
 const u=await user();
 if(!u){location.replace("login.html?next="+encodeURIComponent(location.pathname.split("/").pop()||"index.html"));return new Promise(()=>{})}
 const me=await whoAmI(u);
 if(me.blocked){await logout();return new Promise(()=>{})}
 if(!allowed.includes(me.role)){location.replace(HOME[me.role]);return new Promise(()=>{})}
 return me;
}
// index.html uses this: send everyone to the right place.
export async function route(){
 const u=await user();if(!u){location.replace("login.html");return}
 const me=await whoAmI(u);if(me.blocked){await logout();return}
 location.replace(HOME[me.role]);
}

// ---------- shared UI: layout shell, icons, dialogs, toast ----------
const IC={home:'<path d="M3 11l9-8 9 8M5 10v10h14V10"/>',users:'<circle cx="9" cy="8" r="3.5"/><path d="M2 20c0-3.5 3-6 7-6s7 2.5 7 6M16 4.5a3.5 3.5 0 010 7M18 14c2.5.6 4 2.5 4 6"/>',star:'<path d="M12 3l2.7 5.6 6.1.9-4.4 4.3 1 6.1L12 17l-5.4 2.9 1-6.1L3.2 9.5l6.1-.9z"/>',link:'<path d="M10 14a4 4 0 005.7 0l3-3a4 4 0 00-5.7-5.7l-1 1M14 10a4 4 0 00-5.7 0l-3 3a4 4 0 005.7 5.7l1-1"/>',server:'<rect x="3" y="4" width="18" height="7" rx="2"/><rect x="3" y="13" width="18" height="7" rx="2"/><path d="M7 7.5h.01M7 16.5h.01"/>',chart:'<path d="M4 20V10M10 20V4M16 20v-7M22 20H2"/>',out:'<path d="M9 4H5a2 2 0 00-2 2v12a2 2 0 002 2h4M16 8l4 4-4 4M20 12H9"/>',wallet:'<rect x="3" y="6" width="18" height="14" rx="3"/><path d="M3 10h18M16 15h2"/>'};
export const icon=(n,s=19)=>`<svg width="${s}" height="${s}" viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round">${IC[n]||""}</svg>`;
export const ini=n=>(n||"?").trim().split(/\s+/).map(w=>w[0]).slice(0,2).join("").toUpperCase();
export const dt=t=>t?.toDate?t.toDate().toLocaleDateString(undefined,{day:"numeric",month:"short",year:"numeric"}):"–";
export function toast(m,bad){const t=document.getElementById("toast");t.textContent=m;t.className="on"+(bad?" bad":"");clearTimeout(t._t);t._t=setTimeout(()=>t.className="",2800)}
export function dlg(html,cls=""){const d=document.createElement("div");d.className="ov "+cls;d.innerHTML=`<div class="dl">${html}</div>`;d.addEventListener("click",e=>{if(e.target===d)d.remove()});document.body.appendChild(d);return d}
export const closeAll=()=>document.querySelectorAll(".ov").forEach(o=>o.remove());
export const ask=(t,p,ok="Confirm",bad)=>new Promise(r=>{const d=dlg(`<h3>${t}</h3><p>${p}</p><div class="ra"><button class="b" data-n>Cancel</button><button class="b ${bad?"dn f":"pr"}" data-y>${ok}</button></div>`);d.addEventListener("click",e=>{if(e.target===d||"n"in e.target.dataset){d.remove();r(false)}if("y"in e.target.dataset){d.remove();r(true)}})});
// items: [{id,label,icon,title,sub}] - each id matches a <section data-v="id"> inside <div id="views">
export function mount(me,{items,extra="",actions=""}){
 const v=document.getElementById("views"),nm=me.data.displayName||me.u.email,hooks={};
 document.body.insertAdjacentHTML("afterbegin",`<div class="app"><aside class="side"><div class="brand"><i>G</i><b>Gomez Trading</b></div>${items.map(i=>`<button class="ni" data-n="${i.id}">${icon(i.icon)}${i.label}<span class="bd" hidden></span></button>`).join("")}<div class="sp"></div>${extra}</aside><div class="main"><header class="top"><div><h1 id="pt"></h1><p id="ps"></p></div><div class="sp"></div>${actions}<div class="me"><span class="av">${esc(ini(nm))}</span><span class="mt"><b>${esc(nm)}</b><small>${me.role[0].toUpperCase()+me.role.slice(1)}</small></span></div><button class="b sm" id="lo">${icon("out",15)}<span class="hd">Sign out</span></button></header><div id="slot"></div></div><nav class="bot">${items.map(i=>`<button data-n="${i.id}">${icon(i.icon,22)}${i.label}<span class="bd" hidden></span></button>`).join("")}</nav></div><div id="toast"></div>`);
 document.getElementById("slot").appendChild(v);
 document.getElementById("lo").onclick=logout;
 const show=id=>{const it=items.find(i=>i.id===id)||items[0];v.querySelectorAll("[data-v]").forEach(s=>s.hidden=s.dataset.v!==it.id);document.querySelectorAll("[data-n]").forEach(b=>b.classList.toggle("on",b.dataset.n===it.id));document.getElementById("pt").textContent=it.title;document.getElementById("ps").textContent=it.sub;if(location.hash!=="#"+it.id)history.replaceState(null,"","#"+it.id);scrollTo(0,0);hooks[it.id]?.()};
 document.querySelectorAll("[data-n]").forEach(b=>b.onclick=()=>show(b.dataset.n));
 addEventListener("hashchange",()=>show(location.hash.slice(1)));
 const badge=(id,n)=>document.querySelectorAll(`[data-n="${id}"] .bd`).forEach(b=>{b.hidden=!n;b.textContent=n});
 return{show,hooks,badge};
}

// ---------- fees ----------
export const FEE_DEFAULT=5; // % of profit above the high-water mark (per trader, editable by admin)
export const money2=n=>"$"+(+n||0).toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
// fee building up on a copy link = rate x (equity - high-water mark), never negative
// effective high-water mark = the higher of the stored mark and the amount the investor put in (nothing is charged until they are above what they invested)
export const hwmOf=f=>f&&f.hwm!=null?Math.max(+f.hwm||0,+f.amount||0):null;
export const accrued=f=>f&&f.status==="active"&&f.equity!=null&&f.hwm!=null?Math.max(0,f.equity-hwmOf(f))*(f.feePct??FEE_DEFAULT)/100:0;

// ---------- password encryption (RSA-OAEP, in the browser) ----------
// Investor pages encrypt with the admin's PUBLIC key; only the admin's private key (kept in the admin's browser) can decrypt.
const b64=u=>btoa(String.fromCharCode(...new Uint8Array(u))),unb64=s=>Uint8Array.from(atob(s),c=>c.charCodeAt(0)),ALG={name:"RSA-OAEP",hash:"SHA-256"};
export async function pwGenKeys(){const k=await crypto.subtle.generateKey({...ALG,modulusLength:2048,publicExponent:new Uint8Array([1,0,1])},true,["encrypt","decrypt"]);return{pub:await crypto.subtle.exportKey("jwk",k.publicKey),priv:await crypto.subtle.exportKey("jwk",k.privateKey)}}
export async function pwEncrypt(pub,text){const k=await crypto.subtle.importKey("jwk",pub,ALG,false,["encrypt"]);return b64(await crypto.subtle.encrypt({name:"RSA-OAEP"},k,new TextEncoder().encode(text)))}
export async function pwDecrypt(priv,s){const k=await crypto.subtle.importKey("jwk",priv,ALG,false,["decrypt"]);return new TextDecoder().decode(await crypto.subtle.decrypt({name:"RSA-OAEP"},k,unb64(s)))}
