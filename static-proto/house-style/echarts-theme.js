/* ============================================================
   ЧИСТЫЙ ЛИСТ - тема ECharts 'cleanleaf' + хелперы чисел.
   Подключать ПОСЛЕ echarts.min.js и tokens.css:
     <script src="house-style/assets/echarts.min.js"></script>
     <link  rel="stylesheet" href="house-style/tokens.css">
     <script src="house-style/echarts-theme.js"></script>
   Использование:  echarts.init(el, 'cleanleaf')
   Палитра по умолчанию - МОНОХРОМ по рангу (тёмный = крупнее).
   Парный YoY (прошлый/текущий) - цвета задавать вручную:
     CLEANLEAF.c.accentSoft (прошлый год) + CLEANLEAF.c.accent (текущий).
   ============================================================ */
(function(){
 if(typeof echarts==='undefined'){console.warn('cleanleaf: подключите echarts.min.js до темы');return;}
 var css=getComputedStyle(document.documentElement);
 var V=function(name,fallback){var v=css.getPropertyValue(name).trim();return v||fallback;};

 var c={
  bg:V('--bg','#f6f7f9'), panel:V('--panel','#ffffff'), ink:V('--ink','#16191d'),
  muted:V('--muted','#5b636b'), faint:V('--faint','#7a828b'), line:V('--line','#e6e9ec'),
  accent:V('--accent','#2d4a5e'), accentSoft:V('--accent-soft','#93a7b4'),
  good:V('--good','#1f7a4d'), bad:V('--bad','#c0392b'), plan:V('--plan','#9aa3ab')
 };
 // монохром по рангу (для donut/treemap/групп/каналов)
 var ranks=[V('--rank-1','#2d4a5e'),V('--rank-2','#4a6577'),V('--rank-3','#6b8492'),
            V('--rank-4','#8ea4ae'),V('--rank-5','#b4c3cb'),V('--rest','#d7dde1')];
 var FONT='PT Sans Narrow';

 var axisCommon={
  axisLine:{lineStyle:{color:c.line}},
  axisTick:{show:false},
  axisLabel:{color:c.muted,fontSize:14,fontFamily:FONT},
  splitLine:{lineStyle:{color:c.line,type:'dashed'}}
 };

 echarts.registerTheme('cleanleaf',{
  color:ranks,
  backgroundColor:'transparent',
  textStyle:{fontFamily:FONT,color:c.ink},
  title:{textStyle:{fontFamily:FONT,color:c.ink,fontWeight:700},
         subtextStyle:{color:c.muted,fontFamily:FONT}},
  legend:{textStyle:{color:c.muted,fontSize:14,fontFamily:FONT},
          itemWidth:16,itemHeight:10},
  grid:{left:60,right:18,top:36,bottom:34,containLabel:true},
  categoryAxis:axisCommon, valueAxis:axisCommon, logAxis:axisCommon, timeAxis:axisCommon,
  tooltip:{backgroundColor:c.panel,borderColor:c.line,borderWidth:1,confine:true,
           textStyle:{color:c.ink,fontFamily:FONT,fontSize:16},
           axisPointer:{type:'shadow',shadowStyle:{color:'rgba(45,74,94,.06)'}}},
  bar:{barCategoryGap:'34%',itemStyle:{borderRadius:[2,2,0,0]}},
  pie:{itemStyle:{borderColor:c.panel,borderWidth:2}},
  line:{lineStyle:{width:2},symbol:'circle',symbolSize:6}
 });

 /* ---------- ХЕЛПЕРЫ ЧИСЕЛ (RU) ----------
    масштабировать крупные суммы: «23,1 млн ₽», а не «23 079 197 ₽». */
 var ru =function(n){return Math.round(n).toLocaleString('ru-RU');};
 var ru1=function(n){return (Math.round(n*10)/10).toLocaleString('ru-RU',
          {minimumFractionDigits:1,maximumFractionDigits:1});};
 function mln(v){var s=(v/1e6).toFixed(1).replace('.',',');return s.endsWith(',0')?s.slice(0,-2):s;}
 function money(v){var a=Math.abs(v);
  if(a>=1e6)return mln(v)+' млн ₽';
  if(a>=1e3)return ru(v/1e3)+' тыс ₽';
  return ru(v)+' ₽';}
 function pct(v){return (v>0?'+':'')+(Math.round(v*10)/10).toString().replace('.',',')+'%';}
 function pctInt(v){return (v>0?'+':'')+Math.round(v)+'%';}   // YoY на карточках = целый %

 window.CLEANLEAF={c:c,ranks:ranks,font:FONT,
  fmt:{ru:ru,ru1:ru1,mln:mln,money:money,pct:pct,pctInt:pctInt}};
})();
