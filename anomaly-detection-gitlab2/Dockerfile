FROM centos/python-36-centos7:20191101-e7373f1

USER root
RUN yum install -y python-devel build-essentials gcc gcc-c++

ADD requirements.txt /

RUN pip install --upgrade pip
RUN pip install -r /requirements.txt
#ADD environment.yml /
ADD app.py /
ADD prometheus.py /
ADD model.py /
ADD ceph.py /
ADD lib /lib
ADD templates /templates

#RUN templates copy
#RUN chmod g+w /etc/passwd
#RUN conda env create -f /environment.yml

CMD [ "python", "/app.py"]
