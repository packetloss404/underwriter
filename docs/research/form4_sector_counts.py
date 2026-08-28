import json, collections
d=json.load(open('pbuys.json')); sic=json.load(open('sic.json'))
agg=d['agg']

def sector(s):
    if not s: return None
    s=int(s)
    if 6020<=s<=6036 or 6120<=s<=6199 or 6200<=s<=6299 or 6300<=s<=6411 or 6500<=s<=6553 or 6770<=s<=6799 or s==6726: return ('Financials & Real Estate','XLF')
    if 1300<=s<=1389 or s in (2911,5171,5172,4922,4923,4924): return ('Energy','XLE')
    if s in (3674,3559,3672,3827,3826,3825): return ('Semiconductors','SMH')
    if 7370<=s<=7379 or 3570<=s<=3579 or 3660<=s<=3669 or 3670<=s<=3699 or s in (3661,3663,3669,7385): return ('Technology','XLK')
    if 2833<=s<=2836 or 8000<=s<=8093 or s in (3841,3842,3843,3844,3845,5122,8731,5047,5912): return ('Health Care','XLV')
    if 3720<=s<=3728 or s in (3480,3760,3761,3764,3769,3812): return ('Aerospace & Defense','ITA')
    if 4900<=s<=4991: return ('Utilities','XLU')
    if 2800<=s<=2899 or 1000<=s<=1099 or 1400<=s<=1499 or 2600<=s<=2699 or 3300<=s<=3399 or s in (3241,2650,3050): return ('Materials','XLB')
    if 5200<=s<=5990 or 7000<=s<=7011 or 5812<=s<=5813 or 7900<=s<=7999 or 2300<=s<=2399 or 3711<=s<=3716 or 3140<=s<=3149 or s in (7011,7990,8200): return ('Consumer Discretionary','XLY')
    if 2000<=s<=2111 or 5140<=s<=5149 or s in (5411,5412,2840,2844,5122): return ('Consumer Staples','XLP')
    if 3400<=s<=3569 or 1600<=s<=1799 or 4200<=s<=4231 or 3580<=s<=3599 or 7300<=s<=7389 or 8700<=s<=8748 or 4400<=s<=4789 or 3580<=s<=3629: return ('Industrials','XLI')
    return ('Unmapped / other','—')

days=sorted({k.split('|')[0] for k in agg}); nweeks=len(days)/5.0
tot=collections.Counter(); k100=collections.Counter(); lst=collections.Counter(); unk=0
for k,v in agg.items():
    cik=k.split('|')[1]; s=sic.get(cik,{})
    if 'err' in s: unk+=1; continue
    sec=sector(s.get('sic'))
    if sec is None: unk+=1; continue
    tot[sec]+=1
    if v>=100000:
        k100[sec]+=1
        if any(e in ('NYSE','Nasdaq','NYSE American','NYSEAmerican') for e in (s.get('exch') or [])): lst[sec]+=1
N=sum(tot.values())
print(f"2026Q2: {len(days)} filing days = {nweeks:.1f} weeks; {N} classified P-events ({unk} unclassified)\n")
hdr=f"| Sector (SIC-grouped) | ETF | P-events/qtr | **/week** | ≥$100k /week | listed ≥$100k /week |"
print(hdr); print("|---|---|---|---|---|---|")
for sec,_ in tot.most_common():
    name,etf=sec
    print(f"| {name} | {etf} | {tot[sec]} | **{tot[sec]/nweeks:.1f}** | {k100[sec]/nweeks:.1f} | {lst[sec]/nweeks:.1f} |")
print(f"| **TOTAL** | | **{N}** | **{N/nweeks:.1f}** | **{sum(k100.values())/nweeks:.1f}** | **{sum(lst.values())/nweeks:.1f}** |")
banks=sum(1 for k in agg if (sic.get(k.split('|')[1],{}).get('sic') or '').isdigit() and 6020<=int(sic[k.split('|')[1]]['sic'])<=6036)
cef  =sum(1 for k in agg if (sic.get(k.split('|')[1],{}).get('sic') or '')=='6726')
print(f"\ndepository institutions (SIC 6020-6036): {banks} of {N} = {banks/N*100:.1f}%")
print(f"investment offices / CEFs (SIC 6726):    {cef} of {N} = {cef/N*100:.1f}%")
