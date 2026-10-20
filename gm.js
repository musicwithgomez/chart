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
