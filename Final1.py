import re
import pandas as pd
import openai
import os
from flask import Flask, render_template, request, jsonify, send_file
from dotenv import load_dotenv
from io import BytesIO
from threading import Timer
import datetime
import unicodedata

load_dotenv()
openai.api_key = os.getenv("OPENAI_API_KEY")
app = Flask(__name__)
temp_output = "gecici_output.xlsx"
auto_save_interval = 15 * 60
current_df = pd.DataFrame()
sozluk_dict = {}
compiled_pattern = None

# Türkçeye özel büyük/küçük harf dönüşümü
def to_upper_tr(text):
    # Sadece Türkçede doğru büyük İ, sonradan normalize!
    result = text.replace("i", "İ").replace("ı", "I").upper()
    return unicodedata.normalize('NFC', result)

def to_lower_tr(text):
    return text.replace("I", "ı").replace("İ", "i").lower()



# Kelime bazında harf koruma
def uygula_harf_bicimi(orj_kelime, tr_kelime):
    if orj_kelime.isupper():
        return to_upper_tr(tr_kelime)
    elif orj_kelime.islower():
        return to_lower_tr(tr_kelime)
    elif orj_kelime[0].isupper() and orj_kelime[1:].islower():
        return tr_kelime[:1].upper() + tr_kelime[1:].lower()
    else:
        return tr_kelime

# Multi-word ve single-word (r1) öncelikli sözlük patterni
def derle_sozluk_pattern(sozluk_keys):
    # En uzun terimler en başta, kısalar sonda
    keys = sorted(sozluk_keys, key=lambda x: -len(x))
    # tireli, çoklu, tekli, rakamlı teknik terim hepsi destekli
    regex_terimler = [re.escape(k) for k in keys]
    return re.compile(r'(' + '|'.join(regex_terimler) + r')', flags=re.IGNORECASE)

def temizle(k):
    if not isinstance(k, str):
        k = str(k)
    k = unicodedata.normalize("NFKC", k)
    k = k.replace("\xa0", " ")
    k = re.sub(r"[\s\u200b\t\r\n]+", " ", k)
    k = k.strip().lower()
    k = re.sub(r"^[:=;()\[\]{}]+|[:=;()\[\]{}]+$", "", k)
    return k

# Sözlükle multi-word+kelime eşleştirme ve harf koruma
def cevir_split_smart(metin, sozluk, orj=None):
    if metin is None or not str(metin).strip() or str(metin).strip().lower() == "nan":
        return metin
    text = str(metin)
    pattern = derle_sozluk_pattern(sozluk.keys())
    def smart_replace(match):
        word = match.group(0)
        lower_word = word.lower()
        ceviri = sozluk.get(lower_word, word)
        sonuc = uygula_harf_bicimi(word, ceviri)
        # LOG BURADA
        print(f"[DEBUG] Orjinal: {word} | Sözlük: {lower_word} | Türkçe: {ceviri} | Sonuç: {sonuc}")
        return sonuc
    result = pattern.sub(smart_replace, text)
    return result


def get_teknik_prompt(dil="ing", ekstra_terimler="", orijinal_komut=""):
    ornekler = """
Teknik terimler:
sta = km
pk = km
""" + (f"{ekstra_terimler}\n" if ekstra_terimler else "")

    ceviri_ornekleri = """
Örnek Çeviriler:
İngilizce: Choose L with TYPE for LEVELLED AREA
Türkçe: KAZI için TİP ile L seç

İngilizce: Choose L with TYPE for EMBANKMENT
Türkçe: DOLGU için TİP ile L seç

İngilizce: Choose Plot
İspanyolca: Elija Predio
Türkçe: Parsel seç

İngilizce: Choose line at point for STA
İspanyolca: Elija la línea por el punto para PK
Türkçe: KM için noktadaki çizgiyi seç

İngilizce: End profile ?
İspanyolca: ¿Perfil final?
Türkçe: Son profil ?

İngilizce: Final profile?
İspanyolca: ¿Perfil final?
Türkçe: Nihai profil?

İngilizce: <POINT DB
İspanyolca: <BD PUNTUALES
Türkçe: <NOKTASAL VT
"""
    if dil == "isp":
        ana_prompt = (
            "ISTRAM mühendislik yazılımı için aşağıdaki İspanyolca komutu Türkçeye çevir.\n"
            "Sembolleri aynen koru ve kısa teknik Türkçe kullan.\n\n"
        )
        ana_prompt += ornekler + ceviri_ornekleri
        ana_prompt += f"\nİspanyolca Komut: {orijinal_komut}\nTürkçe Komut:"
    else:
        ana_prompt = (
            "ISTRAM mühendislik yazılımı için aşağıdaki İngilizce komutu Türkçeye çevir.\n"
            "Sembolleri aynen koru ve kısa teknik Türkçe kullan.\n\n"
        )
        ana_prompt += ornekler + ceviri_ornekleri
        ana_prompt += f"\nİngilizce Komut: {orijinal_komut}\nTürkçe Komut:"
    return ana_prompt

def gpt_ceviri(metin, teknik_prompt=None):
    print("GPT çağrıldı:", metin)
    try:
        prompt = teknik_prompt or str(metin)
        resp = openai.ChatCompletion.create(
            model="gpt-4.1-mini-2025-04-14",
            messages=[
                {"role": "system", "content": "Sen teknik çeviri uzmanısın."},
                {"role": "user", "content": prompt}
            ],
            temperature=0.0
        )
        result = resp.choices[0].message.content.strip()
        print("GPT cevabı:", result)
        return result
    except Exception as e:
        print("GPT HATASI:", e)
        return metin

def satirda_ingilizce_var_mi(metin):
    if not metin:
        return False
    return re.search(r'\b[a-zA-Z]{2,}\b', metin) is not None

def teknik_eslesmeleri_bul(cumle, sozluk):
    bulunanlar = []
    for ing_kelime in sozluk.keys():
        if re.search(r"\b" + re.escape(ing_kelime) + r"\b", cumle, re.IGNORECASE):
            bulunanlar.append(f"{ing_kelime} = {sozluk[ing_kelime]}")
    return bulunanlar

def anlamsiz_veya_bozuk_mu(text):
    try:
        text.encode('ascii')
    except UnicodeEncodeError:
        return True
    return False

def auto_save():
    if not current_df.empty:
        df3 = current_df[["İngilizce", "İspanyolca", "Türkçe"]].copy()
        sari_list = list(current_df.get("sari", [False]*len(df3)))
        with pd.ExcelWriter(temp_output, engine="xlsxwriter") as writer:
            df3.to_excel(writer, index=False, sheet_name="Sheet1")
            workbook = writer.book
            worksheet = writer.sheets['Sheet1']
            yellow_format = workbook.add_format({'bg_color': '#FFFF00'})
            for row_idx, is_sari in enumerate(sari_list):
                if is_sari:
                    worksheet.write(row_idx + 1, 2, df3.iloc[row_idx, 2], yellow_format)
    Timer(auto_save_interval, auto_save).start()

def teknik_terim_gpt_harf_koruma(orj_cumle, tr_cumle, sozluk_dict):
    sonuc = tr_cumle
    for ing, tr in sozluk_dict.items():
        matches = list(re.finditer(rf"\b{re.escape(ing)}\b", orj_cumle, flags=re.IGNORECASE))
        for match in matches:
            orj_kelime = match.group(0)
            tr_yeni = uygula_harf_bicimi(orj_kelime, tr)
            sonuc = re.sub(rf"\b{re.escape(tr)}\b", tr_yeni, sonuc, flags=re.IGNORECASE)
    return sonuc




@app.route("/", methods=["GET", "POST"])
def index():
    global current_df, sozluk_dict, compiled_pattern
    if request.method == "POST":
        dosya       = request.files.get("dosya")
        sozluk_file = request.files.get("sozluk")
        if not dosya or not sozluk_file:
            return "Lütfen dosya ve sözlük yükleyin."

        df        = pd.read_excel(dosya)
        df_sozluk = pd.read_excel(sozluk_file)
        df["Türkçe"]        = df.get("Türkçe", "").astype(str)
        df["ceviri_ing_tr"] = ""
        df["ceviri_isp_tr"] = ""

        sozluk_dict = {}
        for _, row in df_sozluk.iterrows():
            k_ing = temizle(row.get("İngilizce",""))
            k_isp = temizle(row.get("İspanyolca",""))
            tr    = str(row.get("Türkçe","")).strip()
            if k_ing and tr:
                sozluk_dict[k_ing] = tr
            if k_isp and tr:
                sozluk_dict[k_isp] = tr

        pattern = derle_sozluk_pattern(sozluk_dict.keys())
        batch_size = 100

        for i in range(0, len(df), batch_size):
            batch_df = df.iloc[i:i+batch_size]
            for j, row in batch_df.iterrows():
                ing = str(row.get("İngilizce", "")).strip()
                isp = str(row.get("İspanyolca", "")).strip()

                # 1. Sözlükle kelime kelime çeviri (ama her durumda GPT'ye de gidecek!)
                sozluk_ceviri_ing = cevir_split_smart(ing, sozluk_dict, orj=ing)
                sozluk_ceviri_isp = cevir_split_smart(isp, sozluk_dict, orj=isp)

                # 2. DAİMA GPT'ye gönder!
                eslesenler_ing    = teknik_eslesmeleri_bul(ing, sozluk_dict)
                ekstra_prompt_ing = "\n".join(eslesenler_ing)
                teknik_prompt_ing = get_teknik_prompt(
                    "ing", ekstra_terimler=ekstra_prompt_ing, orijinal_komut=ing
                )
                gpt_sonuc_ing = gpt_ceviri(ing, teknik_prompt=teknik_prompt_ing)

                eslesenler_isp    = teknik_eslesmeleri_bul(isp, sozluk_dict)
                ekstra_prompt_isp = "\n".join(eslesenler_isp)
                teknik_prompt_isp = get_teknik_prompt(
                    "isp", ekstra_terimler=ekstra_prompt_isp, orijinal_komut=isp
                )
                gpt_sonuc_isp = gpt_ceviri(isp, teknik_prompt=teknik_prompt_isp)

                # 3. Sözlükle çeviriden farklıysa GPT çıktısını kullan
                if (gpt_sonuc_isp and gpt_sonuc_isp.strip()
                    and gpt_sonuc_isp.strip().lower() != sozluk_ceviri_isp.strip().lower()):
                    turkce = gpt_sonuc_isp
                    orj_satir = isp
                else:
                    turkce = gpt_sonuc_ing
                    orj_satir = ing

                # 4. Teknik terimler için ALL CAPS/büyük harf koruma uygula
                turkce = teknik_terim_gpt_harf_koruma(orj_satir, turkce, sozluk_dict)
                turkce = unicodedata.normalize('NFC', turkce)  # Türkçe karakterleri temizle

                df.at[j, "Türkçe"] = turkce

        current_df = df
        df.head(1000).to_excel(temp_output, index=False)
        return render_template("sonuc.html", result_df=df.head(1000))

    return render_template("index.html")



@app.route("/compile", methods=["GET","POST"])
def compile_patterns_route():
    global compiled_pattern, sozluk_dict
    if not sozluk_dict:
        return jsonify({'error': 'Sözlük yüklenmemiş'}), 400
    compiled_pattern = derle_sozluk_pattern(sozluk_dict.keys())
    return jsonify({'status': 'ok'})

@app.route('/degistir', methods=['POST'])
def degistir():
    global pattern, sozluk_dict

    if 'pattern' not in globals() or pattern is None:
        pattern = derle_sozluk_pattern(sozluk_dict.keys())

    data = request.json
    ing = data.get("ing", "").strip()
    isp = data.get("isp", "").strip()
    mod = data.get("mod", 0)

    if not pattern:
        pattern = derle_sozluk_pattern(sozluk_dict.keys())

    if mod == 0:
        ceviri_metni = isp
        ceviri = cevir_split_smart(ceviri_metni, sozluk_dict, orj=isp)
        if satirda_ingilizce_var_mi(ceviri):
            eslesenler    = teknik_eslesmeleri_bul(ceviri_metni, sozluk_dict)
            ekstra_prompt = "\n".join(eslesenler)
            teknik_prompt = get_teknik_prompt(
                "isp", ekstra_terimler=ekstra_prompt, orijinal_komut=ceviri_metni
            )
            gpt_sonuc = gpt_ceviri(ceviri_metni, teknik_prompt=teknik_prompt)
            if (gpt_sonuc and gpt_sonuc.strip()
                    and gpt_sonuc.strip().lower() != ceviri_metni.strip().lower()):
                ceviri = gpt_sonuc

    elif mod == 1:
        ceviri = ing

    elif mod == 2:
        ceviri_metni = ing
        ceviri = cevir_split_smart(ceviri_metni, sozluk_dict, orj=ing)
        if (not ceviri
                or ceviri.strip().lower() == ceviri_metni.strip().lower()
                or satirda_ingilizce_var_mi(ceviri)):
            eslesenler    = teknik_eslesmeleri_bul(ceviri_metni, sozluk_dict)
            ekstra_prompt = "\n".join(eslesenler)
            teknik_prompt = get_teknik_prompt("ing", ekstra_terimler=ekstra_prompt, orijinal_komut=ceviri_metni)
            gpt_sonuc = gpt_ceviri(ceviri_metni, teknik_prompt=teknik_prompt)
            if (gpt_sonuc and gpt_sonuc.strip()
                    and gpt_sonuc.strip().lower() != ceviri_metni.strip().lower()):
                ceviri = gpt_sonuc

    else:
        ceviri = ""

    return jsonify({"ceviri": ceviri})

@app.route("/kaydet", methods=["POST"])
def kaydet():
    global current_df
    try:
        data = request.form.to_dict()
        dosya_adi = data.get("dosya_adi", f"output_{datetime.datetime.now().strftime('%Y%m%d_%H%M%S')}")
        format = data.get("format", "excel")
        rows = []
        i = 0
        while f"ingilizce_{i}" in data:
            ing = data.get(f"ingilizce_{i}")
            isp = data.get(f"ispanyolca_{i}")
            tr = data.get(f"turkce_{i}")
            sari = str(data.get(f"sari_{i}", "")).lower() == "true"
            ceviri_ing_tr = data.get(f"ceviri_ing_tr_{i}", "")
            ceviri_isp_tr = data.get(f"ceviri_isp_tr_{i}", "")
            rows.append({
                "İngilizce": ing,
                "İspanyolca": isp,
                "Türkçe": tr,
                "ceviri_ing_tr": ceviri_ing_tr,
                "ceviri_isp_tr": ceviri_isp_tr,
                "sari": sari
            })
            i += 1

        df = pd.DataFrame(rows)
        current_df.update(df)

        output = BytesIO()
        if format == "excel":
            with pd.ExcelWriter(output, engine="xlsxwriter") as writer:
                df3 = df[["İngilizce", "İspanyolca", "Türkçe"]].copy()
                df3.to_excel(writer, index=False, sheet_name="Sheet1")
                workbook = writer.book
                worksheet = writer.sheets['Sheet1']
                yellow_format = workbook.add_format({'bg_color': '#FFFF00'})
                for row_idx, is_sari in enumerate(df["sari"]):
                    if is_sari:
                        worksheet.write(row_idx + 1, 2, df3.iloc[row_idx, 2], yellow_format)
        elif format == "html":
            df[["İngilizce", "İspanyolca", "Türkçe"]].to_html(buf=output, index=False)
        elif format == "txt":
            df[["İngilizce", "İspanyolca", "Türkçe"]].to_csv(output, sep="\t", index=False)

        output.seek(0)
        filename = f"{dosya_adi}.{format if format != 'excel' else 'xlsx'}"
        mimetype = "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet" if format == "excel" else "text/html"
        return send_file(output, as_attachment=True, download_name=filename, mimetype=mimetype)

    except Exception as e:
        return f"Kaydetme hatası: {e}"

if __name__ == "__main__":
    auto_save()
    app.run(debug=True)
