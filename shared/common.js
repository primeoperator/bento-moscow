/* Общий код клиентского и кабинетного приложений: настройки кондитерской, меню, расчёт цены и формирование заказа. */
const SHOP_CONFIG={name:"БЕНТО · ИРИНА",city:"Москва",metro:"м. Водный стадион",currency:"₽",apiUrl:"http://localhost:8000"}; // apiUrl: публичный HTTPS-адрес бэкенда (для локальной отладки: http://localhost:8000)
const MENU={
 formats:[{id:'bento',n:'Бенто-торт 500–550 г',d:'на 1–4 человека · от 2 300',p:2300,hw:48,L:2,sh:14,ry:10,e:'🧁'},
  {id:'maxi',n:'Бенто MAXI 1 кг',d:'от 3 800, зависит от дизайна',p:3800,hw:64,L:3,sh:14,ry:12,e:'🎂'},
  {id:'mini',n:'Cake to go MINI',d:'от 400 за шт · наборы 6/9',u:400,packs:[6,9],hw:30,L:2,sh:10,ry:7,e:'🍰'},
  {id:'tgm',n:'Cake to go MAXI',d:'от 1 200 за шт · наборы 1–12',u:1200,packs:[1,2,4,6,9,12],hw:36,L:2,sh:12,ry:8,e:'🍮'}],
 sponges:[['Шоколадный','#6b3a22'],['Ванильный','#f3d9a0'],['Морковно-апельсиновый','#e88a3a'],['Лавандовый','#b9a3e3'],['Фисташковый','#a9cf7e']],
 flavors:[['Клубника','#ff5d7a'],['Вишня','#9c1f45'],['Карамель','#d99a3a'],['Шоколадный крем','#5a3322'],['Манго-маракуйя','#ffc933'],['Малина','#e0245e']],
 designs:[['Без рисунка',0],['Простой декор',300],['Авторский рисунок',700]]};
const TP=[['cherry','🍒','Вишня'],['berry','🍓','Клубника'],['blue','🫐','Черника'],['star','⭐','Звёзды'],['heart','💖','Сердечки'],['rain','🌈','Посыпка']];
const FR=[['Белый','#fff8f0'],['Розовый','#ffb3c8'],['Мятный','#a8e6cf'],['Кремовый','#ffe9b0'],['Лавандовый','#d9ccf5']];
const $=s=>document.querySelector(s),rnd=a=>a[Math.floor(Math.random()*a.length)],clamp=(v,a,b)=>Math.max(a,Math.min(b,v));
const money=n=>String(n).replace(/\B(?=(\d{3})+(?!\d))/g,'\u00a0')+'\u00a0'+SHOP_CONFIG.currency;
const tm=()=>new Date().toLocaleTimeString('ru-RU',{hour:'2-digit',minute:'2-digit'});
const dateRu=d=>d?new Date(d+'T12:00:00').toLocaleDateString('ru-RU',{day:'numeric',month:'long'}):'не указана';
const LS={get(k,d){try{const v=localStorage.getItem(k);return v?JSON.parse(v):d}catch(e){return d}},set(k,v){try{localStorage.setItem(k,JSON.stringify(v))}catch(e){}}};
const d3=new Date(Date.now()+3*864e5).toISOString().slice(0,10);
function priceOf(s){const F=MENU.formats[s.f];return(F.u?F.u*s.pack:F.p)+MENU.designs[s.dz][1]}
function stars(s){const n=Math.min(5,1+s.tp.size+(s.candle?1:0)+(s.txt.trim()?1:0)+(s.dz>1?1:0));return'★'.repeat(n)+'☆'.repeat(5-n)}
function orderOf(s){const F=MENU.formats[s.f],t=s.txt.trim(),tp=TP.filter(x=>s.tp.has(x[0])).map(x=>x[2].toLowerCase()).join(', ')||'без декора',dt=dateRu(s.date);
 const rows=[['Десерт',F.n+(F.packs?', набор '+s.pack+' шт':'')],['Бисквит',MENU.sponges[s.sp][0]],['Вкус',MENU.flavors[s.fl][0]+(s.fl===2?(s.pn?' с арахисом':' без арахиса'):'')+' + крем-чиз'],['Крем',FR[s.fr][0].toLowerCase()],['Декор',tp+(s.candle?', свеча':'')],['Надпись',t?'«'+t+'»':'нет'],['Дизайн',MENU.designs[s.dz][0]+(s.note.trim()?': '+s.note.trim():'')],['Получение',s.dv?'Яндекс Доставка: '+(s.addr.trim()||'адрес уточнить'):'Самовывоз, '+SHOP_CONFIG.metro],['Дата',dt]];
 const k=Math.ceil(F.packs?(F.id==='mini'?s.pack/6:s.pack/2):F.id==='maxi'?2:1);
 return{nick:s.nick,rows,price:priceOf(s),k,short:F.n,wish:rnd(['Хочу '+F.n.toLowerCase()+'!','Нужен торт к '+dt,'Нарисуете рисунок?','Срочно с надписью!'])}}
function randState(nick){const f=Math.floor(Math.random()*4),F=MENU.formats[f];return{f,sp:Math.floor(Math.random()*5),fl:Math.floor(Math.random()*6),pn:Math.random()<.5,fr:Math.floor(Math.random()*5),tp:new Set([rnd(TP)[0]]),candle:Math.random()<.4,txt:rnd(['','С ДР!','Любимой','Мы вместе']),dz:Math.floor(Math.random()*3),note:rnd(['','Кот в очках','Цветы и ягоды']),pack:F.packs?rnd(F.packs):6,nick,dv:Math.random()<.5?1:0,addr:'Москва, ул. Примерная, 1',date:d3}}
