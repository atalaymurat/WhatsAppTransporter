# Liste Yonetimi: Ilerleme ve Yapilacaklar

Bu belge CSV ile WhatsApp gonderim listelerinin mevcut durumunu, yarim kalan islerin nasil ele alinacagini ve gelistirme adimlarini takip eder.

## Uretim Hazirligi

- [x] Flask gelistirme sunucusunu Windows destekli Waitress WSGI sunucusuna gecir; paneli yalnizca `127.0.0.1` uzerinde sun.
- [x] Ayni WhatsApp Chromium profili ve veritabaniyla iki uygulama surecinin ayni anda acilmasini engelle.
- [x] Dosya/istek boyutunu sinirla ve tarayicidan gelen degisiklik isteklerinde farkli kaynaklari reddet.
- [x] SQLite WAL modunu ve sik kullanilan gonderim gecmisi sorgulari icin indeksleri etkinlestir.
- [x] Log tablo semasini her olayda tekrar olusturma; uygulama log ekranini gorunmeyen sekmelerde sorgulama ve gorunen kayit sayisini azalt.
- [x] Gecmis CSV gecisi sirasinda alici kaydini her satirda yeniden taramak yerine telefon esleme haritasi kullan.
- [x] Buyuk log ve gonderim gecmisi disa aktarmalarini bellekte tum sonucu biriktirmeden parca parca yaz.
- [ ] Modulleri arayuz, API, gonderim motoru ve kalici depolama olarak ayir; API veri modellerine tip/alan dogrulamasi ekle.
- [x] Panelin HTML/CSS/JavaScript arayuzunu `templates/index.html` dosyasina tasi; `wa_panel.py` arayuz metni yerine bu sablon dosyasini yuklesin.
- [x] Move phone normalization and CSV/Google Contacts parsing to `csv_import.py`; keep the existing message fingerprint callback.
- [ ] Yedekleme ve geri yukleme akisini kullaniciya sun; veritabani ve WhatsApp oturum dosyalarinin kurtarma adimlarini belge.
- [ ] Otomatik test ve uygulama paketleme/kurulum sureci ekle; temiz Windows ortamina kurulum ve gonderim akisini dogrula.

## Mevcut Durum

- [x] CSV secilince gecerli kayitlar onizlemede listelenir; her kisi icin gonderme secimi vardir.
- [x] Gonderim secilmeyen kisiler gorevde atlananlar olarak tutulur ve indirilebilir.
- [x] Ayni telefon ve ayni sablon/mesaj daha once gonderilmisse normal gonderimde filtrelenir.
- [x] Ayni kisiye ayni mesaj ID'si ikinci kez gonderilmez; eski surumde tekrar gondermek icin olusturulmus alternatif kimlikler de asil mesaj ID'sine tasinir.
- [x] Gonderim gorevleri `jobs.json` dosyasina kaydedilir. Uygulama yeniden acilinca calismakta olan gorev bekliyor durumuna alinir ve kalan alicilardan surer.
- [x] Gonderim denemesi sonucundan once kalici gonderim gecmisine yazilir. Sonucu belirsiz denemeler otomatik olarak tekrar gonderilmez.
- [x] Liste detayinda gonderildi, bekliyor, hata, gecersiz numara, daha once gonderilmis ve atlanan kisiler ayri gorulur.
- [x] Hatalilar tekrar siraya alinabilir; numara duzenlenebilir veya kayit silinebilir. Sorunlu ve atlanan kayitlar CSV olarak indirilebilir.
- [x] Ek CSV yukleyerek mevcut goreve yeni kisiler eklenir; ayni kisi ve mesaj tekrar eklenmez.
- [x] Kesin basarili gonderim alan telefonlar merkezi kisi tablosuna yazilir; eski basarili gonderim gecmisi ilk acilista bu tabloya aktarilir.
- [x] Kisi ile gonderilen mesajlarin `message_id` iliskisi ve ayrintilari gonderim gecmisinde korunur; ayni kisiye ayni mesaj tekrar eklenmez.
- [x] Kisi "mesaj almak istemiyor" olarak isaretlenebilir veya telefon numarasi dogrudan engelleme listesine eklenebilir; bu engel CSV seciminden ve sirada bekleyen gonderimlerden once uygulanir.
- [x] CSV onizlemesi engellenen kisileri gosterir ve secilemez yapar; gonderim filtresi ayni mesaj ID'sini daha once almis kisileri de goreve eklemez.
- [x] Kisi panelinde arama, izin durumu yonetimi, kisi basina mesaj ID/gonderim gecmisi ve tum kisi listesini CSV disa aktarma bulunur.
- [x] Arayuz Ana Panel, Mesaj Gonder, Sablonlar ve Kisiler sekmelerine ayrildi; sekme secimi yenileme sonrasinda korunur.
- [x] Ana Panelde toplam kayitli kisi sayisi ve son 14 gun icin gunluk basarili mesaj alan benzersiz kisi sayisi bar grafigi gosterilir.

## Yapilacaklar

### P1 - Yarim kalan gonderimi guvenle surdurme

- [x] Gorev kartinda son islenen CSV satirini, siradaki kisiyi ve kalan kisi sayisini belirgin goster.
- [x] Uygulama yeniden basladiginda devam eden gorevleri bekliyor durumuna alip hemen surdur; hangi kayittan devam edilecegini arayuzde bildir.
- [x] Gonderimden once devam noktasini ve gonderim rezervasyonunu kalici kaydet. Gorev JSON'unu gecici dosyadan atomik degistir; onceki saglam surumu `.bak` dosyasinda tut ve ana dosya bozuksa yedekten kurtar.
- [ ] Sonuc belirsiz kayitlarda kullaniciya WhatsApp'taki giden mesajla karsilastirabilecegi telefon, mesaj, saat ve gorev bilgisini goster. Kullanici karari verilene kadar otomatik tekrar gonderme.
- [x] Kabul kosulu: uygulama gonderim sirasinda kapatilip acildiginda onceden kesinlesmis kisilere tekrar gondermez; kalanlar gorunur ve kaldigi yerden surer. Yeniden baslatma ve bozuk gorev dosyasindan yedek kurtarma testleri eklendi.

### P1 - Ilerleme ve gonderim zamanini anlama

- [ ] Liste ilerlemesini hem sayi hem yuzde olarak goster: toplam, gonderildi, bekliyor, hata, gecersiz, atlandi.
- [ ] Siradaki gonderimin tahmini zamanini ve gunluk limit nedeniyle bekleme varsa bir sonraki calisma zamanini ayri goster.
- [ ] Gonderim baslangic/bitis zamani, son ilerleme zamani ve duraklatilma nedenini gorev detayinda sakla ve goster.
- [ ] Kabul kosulu: kullanici gorev ekranindan listenin nerede oldugunu, neden bekledigini ve tahmini ne zaman devam edecegini gorebilir.

### P1 - Basarisiz ve belirsiz gonderimleri yonetme

- [x] WhatsApp'in sohbet acilirken gosterdigi gecici "Starting chat / Cancel" penceresini gonderim hatasi sayma; sohbet mesaj kutusu hazir olana kadar bekle.
- [x] WhatsApp'in "isn't on WhatsApp" uyarisini gecersiz numara olarak siniflandir; otomatik tekrar etme, eski hata kayitlarini da gecersize cevir ve gecersiz numaraya tekrar dene dugmesi gosterme.
- [x] Gecici sohbet hatasinda ilk otomatik tekrar denemesini 20 saniye sonra planla; normal gonderim bekleme araligini degistirme.
- [ ] Tekrar dene islemi sadece secilen hata kayitlarini yeniden siraya alsin; belirsiz sonuc icin kullanicidan ayri onay istesin.
- [ ] Tekrar deneme sayisini, son hata mesajini ve her deneme zamanini alici gecmisinde sakla.
- [ ] Kabul kosulu: tekrar deneme, zaten gonderilmis kaydi atlar; kullanici hangi kaydin neden tekrar denendigini gorebilir.

### P2 - Hatali veya ulasilamayan telefonlari duzeltme

- [ ] CSV onizlemesinde ulke kodu, numara uzunlugu ve normalizasyon sonucunu gonderimden once goster; gecersiz satiri gonderilebilir secimden cikar.
- [ ] WhatsApp'in numarayi tanimamadigi durum ile sohbet acilamadi/ag baglantisi hatasini ayri siniflandir. Teknik hata, numaranin gecersiz oldugu seklinde raporlanmasin.
- [ ] Sorunlu kayitta eski numara, duzeltilmis numara, hata nedeni ve duzeltme zamani gorunsun. Duzeltilen numara icin gonderim tekrari kullanici kontrolunde olsun.
- [ ] Sorunlu kayitlari CSV/Google Kisiler formatinda disari aktarirken hata etiketlerini koru; duzeltilen numarayi kaynak listeye geri aktarilabilir kil.
- [ ] Kabul kosulu: hatali numara sessizce kaybolmaz; kullanici duzeltir, siler veya daha sonra yeniden inceler.

### P2 - Liste yasam dongusu ve arsiv

- [ ] Her CSV yuklemesi icin dosya adi, yukleme zamani, kaynak satir sayisi ve gorev kimligini sakla.
- [ ] Bir gorev icin ilk CSV, sonradan eklenen dosyalar ve secim/atlama kararlarini birbirinden ayirt edilebilir sekilde kaydet.
- [ ] Tamamlanan gorevleri arsivleme ve arsivden ayrintilari/raporu tekrar acma secenegi ekle.
- [ ] Gorev silme islemi ile gonderim gecmisini silme islemini ayri tut; gecmis varsayilan olarak korunmali.
- [ ] Kabul kosulu: kullanici bir listenin nereden geldigini, hangi kisilerin eklendigini/atlandigini ve sonuc raporunu sonradan bulabilir.

### P1 - Kisi kaydi, mesaj tekrari ve izinleri

- [x] Yalnizca kesin basarili gonderimler kisi kaydina eklenir; onceki basarili gonderimler veritabani gecisiyle korunur.
- [x] Telefon numarasi kisi kaydinda tekildir; her gonderim `send_history` uzerinden mesaj ID'si, zaman, gorev ve sonucuyla bu numaraya baglanir.
- [x] Mesaj ID'si bazli tekrar filtresi gelen CSV kayitlarini eler; kisi izin vermiyorsa mesaj ID'sinden bagimsiz olarak tum mesajlar engellenir.
- [x] Kullanici kisi kaydinda mesaj iznini kaldirabilir/geri verebilir; izin listesi aranabilir, mesaj gecmisi incelenebilir ve CSV olarak indirilebilir.
- [x] Kabul kosulu: engelli kisi CSV onizlemesinde gonderime secilemez ve gorev onceden olusturulmussa dahi gonderim adiminda atlanir.

### P2 - Gonderim loglarini analiz etme

- [x] Uygulama ve gonderim olaylarini SQLite veritabaninda kalici sakla; loglari panelde ara ve CSV olarak indir.
- [x] Gonderim loglarina olay seviyesi, gorev kimligi ve telefon bilgisini ekle.
- [x] Uygulama loglarini ana panelde gorev listesinin hemen altina ve gonderim gecmisinin ustune tasi; WhatsApp baglantisi, alici/izin/tekrar kontrolleri, sohbet acma, icerik hazirlama, gonderme, sonuc dogrulama, bekleme ve hata adimlarini logla.

## Uygulama Sirasi

1. Yarim kalan gorev icin acik devam noktasi ve belirsiz sonuc ekrani.
2. Gorev ilerleme, kalan sure ve bekleme nedeninin iyilestirilmesi.
3. Hata siniflari ve kontrollu tekrar deneme akisi.
4. Telefon onizleme/duzeltme ve kaynak listeye geri aktarma.
5. Yukleme kaydi ve tamamlanan gorev arsivi.

## Is Kurallari

- Bir telefon + sablon kimligi, sablon duzenlense bile ayni kampanya sayilir; onceki gonderim varsayilan olarak tekrar edilmez.
- Ayni kisi + ayni mesaj ID'si kontrolu tekrar gonderim secenegiyle atlanamaz.
- Sonucu belirsiz gonderim, kullanici karari veya guvenilir WhatsApp dogrulamasi olmadan otomatik tekrar edilmez.
- Mesaj istememe kaydi, gonderim gecmisi veya kullanicinin tekrar gonderim seceneginden bagimsiz olarak her zaman onceliklidir.
- CSV'deki gonderim secimi, gorev kaydina kopyalanir; sonradan CSV dosyasinin degismesi olusturulmus gorevi degistirmez.
